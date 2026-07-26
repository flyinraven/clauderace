"""A circuit must never hand back a station this candidate has already sat.

Repeating a known case tests recall, not reasoning. The only way back into the
pool is clearing the attempt, and that is per candidate: one user's practice
must not close a station off for anyone else.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, OsceSession, OsceStation
from app.services.osce.circuit import build_circuit

SUBS = ["Cataract", "Glaucoma", "Cornea & External Eye"]


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        for i in range(12):
            session.add(
                OsceStation(
                    title=f"Station {i}",
                    subspecialty=SUBS[i % len(SUBS)],
                    total_marks=20,
                    source="generated",
                    status="approved",
                    prompts_status="complete",
                    prompts=[{"label": "A", "text": "?", "seconds": 60, "rubric": []}],
                )
            )
        session.commit()
        yield session


def sit(db: Session, user_id: int, station_id: int) -> None:
    db.add(OsceSession(user_id=user_id, station_id=station_id))
    db.commit()


def test_circuit_excludes_stations_already_sat(db: Session) -> None:
    first = build_circuit(db, user_id=1, station_count=3)
    for station_id in first.station_ids:
        sit(db, 1, station_id)

    second = build_circuit(db, user_id=1, station_count=3)
    assert not set(second.station_ids) & set(first.station_ids)


def test_attempts_are_per_candidate(db: Session) -> None:
    circuit = build_circuit(db, user_id=1, station_count=3)
    for station_id in circuit.station_ids:
        sit(db, 1, station_id)

    # A different candidate has sat nothing, so every station is still open.
    other = build_circuit(db, user_id=2, station_count=3)
    assert set(other.station_ids) & set(circuit.station_ids)


def test_clearing_an_attempt_returns_the_station(db: Session) -> None:
    station_id = build_circuit(db, user_id=1, station_count=1).station_ids[0]
    sit(db, 1, station_id)

    # Exhaust everything else so the only candidate left is the cleared one.
    for other in db.query(OsceStation).all():
        if other.id != station_id:
            sit(db, 1, other.id)
    with pytest.raises(ValueError):
        build_circuit(db, user_id=1, station_count=1)

    for sitting in db.query(OsceSession).filter_by(user_id=1, station_id=station_id).all():
        db.delete(sitting)
    db.commit()

    assert build_circuit(db, user_id=1, station_count=1).station_ids == [station_id]


def test_short_circuit_rather_than_a_padded_one(db: Session) -> None:
    """Nine asked for, four left: four returned, none of them repeats."""
    all_ids = [s.id for s in db.query(OsceStation).all()]
    for station_id in all_ids[:8]:
        sit(db, 1, station_id)

    circuit = build_circuit(db, user_id=1, station_count=9)
    assert sorted(circuit.station_ids) == sorted(all_ids[8:])
