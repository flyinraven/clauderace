"""Circuits: a run of nine stations, and how a candidate is doing through one."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from app.api.deps import CurrentUser, DbSession
from app.constants import ROLE_ADMIN
from app.models import OsceCircuit, OsceResult, OsceSession, OsceStation
from app.services.osce.circuit import build_circuit, circuit_progress
from app.services.settings_store import SettingsStore

router = APIRouter()


# --- Circuits -------------------------------------------------------------
class CircuitOut(BaseModel):
    id: int
    title: str
    scheduled_for: date | None
    station_ids: list[int]
    status: str
    progress: dict[str, Any] = {}
    created_at: datetime


class CreateCircuitRequest(BaseModel):
    station_count: int = Field(default=9, ge=1, le=18)
    scheduled_for: date | None = None
    # Sit one paper in its own right - "2026 Semester 1" - rather than a mixed
    # circuit. A paper with eighteen stations gives nine now and nine next time,
    # because the ones already sat are never drawn again.
    exam_period: str | None = None


@router.get("/circuits", response_model=list[CircuitOut])
def list_circuits(user: CurrentUser, db: DbSession) -> list[CircuitOut]:
    stmt = select(OsceCircuit).order_by(OsceCircuit.id.desc())
    if user.role != ROLE_ADMIN:
        stmt = stmt.where(OsceCircuit.user_id == user.id)
    circuits = db.execute(stmt).scalars().all()
    return [
        CircuitOut(
            id=c.id, title=c.title, scheduled_for=c.scheduled_for,
            station_ids=c.station_ids or [], status=c.status,
            progress=circuit_progress(db, c), created_at=c.created_at,
        )
        for c in circuits
    ]


@router.post("/circuits", response_model=CircuitOut, status_code=status.HTTP_201_CREATED)
def create_circuit(
    payload: CreateCircuitRequest, user: CurrentUser, db: DbSession
) -> CircuitOut:
    count = payload.station_count or SettingsStore(db).get_int("osce.stations_per_circuit", 9)
    try:
        circuit = build_circuit(
            db, user.id, count, payload.scheduled_for, payload.exam_period
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return CircuitOut(
        id=circuit.id, title=circuit.title, scheduled_for=circuit.scheduled_for,
        station_ids=circuit.station_ids or [], status=circuit.status,
        progress=circuit_progress(db, circuit), created_at=circuit.created_at,
    )


@router.delete("/circuits/{circuit_id}", status_code=status.HTTP_200_OK)
def delete_circuit(circuit_id: int, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    """Remove a circuit from the list, keeping every sitting it ran.

    A circuit is a plan - nine stations chosen for one day - not a record of
    work. Deleting it must not take the candidate's recorded answers and marks
    with it, so its sittings are detached and survive as ordinary attempts at
    those stations. The model would otherwise cascade them away, which is the
    one outcome nobody would ask for when tidying a list.

    Clearing the attempts as well is `DELETE /stations/{id}/attempts`, which
    says what it does.
    """
    circuit = db.get(OsceCircuit, circuit_id)
    if circuit is None:
        raise HTTPException(status_code=404, detail="Circuit not found")
    if circuit.user_id != user.id and user.role != ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="That circuit belongs to someone else")

    sittings = db.execute(
        select(OsceSession).where(OsceSession.circuit_id == circuit_id)
    ).scalars().all()
    for sitting in sittings:
        sitting.circuit_id = None
    # Detach before deleting: the relationship is delete-orphan, so a circuit
    # deleted with its sittings still attached takes them, their transcripts
    # and their marks with it.
    db.flush()
    circuit.sittings = []
    db.delete(circuit)
    db.commit()
    return {"deleted": circuit_id, "sittings_kept": len(sittings)}


REST_SECONDS = 120


def _circuit_next(db: Session, sitting: OsceSession, user: CurrentUser) -> dict[str, Any] | None:
    """The next station of this candidate's circuit, or None if there is none.

    Returns the rest interval with it. Two minutes between stations is what the
    real circuit gives, and a candidate who is ready sooner may start early -
    so this is a suggestion the client counts down, not a lock.
    """
    if sitting.circuit_id is None:
        return None
    circuit = db.get(OsceCircuit, sitting.circuit_id)
    if circuit is None:
        return None

    order: list[int] = list(circuit.station_ids or [])
    sat = {
        s.station_id
        for s in db.execute(
            select(OsceSession).where(
                OsceSession.circuit_id == circuit.id,
                OsceSession.user_id == user.id,
                OsceSession.submitted_at.is_not(None),
            )
        ).scalars().all()
    }
    remaining = [sid for sid in order if sid not in sat]
    return {
        "circuit_id": circuit.id,
        "title": circuit.title,
        "position": len(order) - len(remaining),
        "stations": len(order),
        "next_station_id": remaining[0] if remaining else None,
        "rest_seconds": REST_SECONDS,
        "finished": not remaining,
    }


@router.get("/circuits/{circuit_id}/results")
def circuit_results(circuit_id: int, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    """Every station's mark, once the whole circuit has been sat.

    Held back deliberately. Seeing station 3's result before sitting station 4
    is not how the day works, and it changes how the rest is answered.
    """
    circuit = db.get(OsceCircuit, circuit_id)
    if circuit is None:
        raise HTTPException(status_code=404, detail="Circuit not found")
    if circuit.user_id != user.id and user.role != ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="That circuit belongs to someone else")

    order: list[int] = list(circuit.station_ids or [])
    sittings = {
        s.station_id: s
        for s in db.execute(
            select(OsceSession).where(
                OsceSession.circuit_id == circuit.id, OsceSession.user_id == user.id
            )
        ).scalars().all()
    }
    results = {
        r.session_id: r
        for r in db.execute(
            select(OsceResult).where(
                OsceResult.session_id.in_([s.id for s in sittings.values()] or [0])
            )
        ).scalars().all()
    }

    stations = []
    for station_id in order:
        station = db.get(OsceStation, station_id)
        sitting = sittings.get(station_id)
        result = results.get(sitting.id) if sitting else None
        stations.append({
            "station_id": station_id,
            "sitting_id": sitting.id if sitting else None,
            "title": (station.title if station else None)
            or (
                f"Station {station.station_label or station.station_number}"
                if station and (station.station_label or station.station_number)
                else None
            ),
            "subspecialty": station.subspecialty if station else None,
            "submitted": bool(sitting and sitting.submitted_at),
            # "queued" and "running" both mean the marking has not landed yet;
            # the summary says so rather than showing a zero.
            "grading_status": sitting.grading_status if sitting else "not_sat",
            "awarded": result.total_awarded if result else None,
            "available": result.total_available if result else None,
        })

    marked = [s for s in stations if s["awarded"] is not None]
    return {
        "circuit_id": circuit.id,
        "title": circuit.title,
        "stations": stations,
        "complete": all(s["submitted"] for s in stations) if stations else False,
        "total_awarded": sum(s["awarded"] for s in marked) if marked else 0,
        "total_available": sum(s["available"] for s in marked) if marked else 0,
        "awaiting_marking": [s["station_id"] for s in stations
                             if s["submitted"] and s["awarded"] is None],
    }
