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

    # A different candidate has sat nothing, so every station is still open to
    # them. Asked for all 12, they get all 12 - including the ones user 1 sat.
    other = build_circuit(db, user_id=2, station_count=12)
    assert set(circuit.station_ids) <= set(other.station_ids)
    assert len(other.station_ids) == 12


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


def test_a_circuit_can_sit_one_paper_in_its_own_right(db, student):
    """"Practise 2026 Semester 1" is a different request from "give me a circuit".

    A real paper is not one station per subspecialty, so that shaping is wrong
    here: the stations come from that sitting alone, in the order they were
    numbered, and an eighteen-station paper gives nine now and nine next time.
    """
    from app.services.osce.circuit import build_circuit
    from tests.test_api_osce import make_station

    for number in range(1, 13):
        make_station(db, station_number=number, exam_period="2026 Semester 1",
                     subspecialty="Cataract")
    for number in range(1, 4):
        make_station(db, station_number=number, exam_period="2025 Semester 2",
                     subspecialty="Glaucoma")

    circuit = build_circuit(db, student.id, 9, exam_period="2026 Semester 1")
    assert len(circuit.station_ids) == 9
    assert "2026 Semester 1" in circuit.title

    from app.models import OsceStation
    chosen = [db.get(OsceStation, i) for i in circuit.station_ids]
    assert {s.exam_period for s in chosen} == {"2026 Semester 1"}, "one paper only"
    assert [s.station_number for s in chosen] == list(range(1, 10)), "in paper order"


def test_the_next_circuit_of_a_paper_continues_where_the_last_stopped(db, student):
    """Twelve stations means nine, then the remaining three - never a repeat."""
    from app.models import OsceSession, OsceStation
    from app.services.osce.circuit import build_circuit
    from tests.test_api_osce import make_station

    for number in range(1, 13):
        make_station(db, station_number=number, exam_period="2026 Semester 1",
                     subspecialty="Cataract")

    first = build_circuit(db, student.id, 9, exam_period="2026 Semester 1")
    for station_id in first.station_ids:
        db.add(OsceSession(user_id=student.id, station_id=station_id))
    db.commit()

    second = build_circuit(db, student.id, 9, exam_period="2026 Semester 1")
    assert set(second.station_ids).isdisjoint(first.station_ids)
    assert len(second.station_ids) == 3, "what is left of the paper"
    assert [db.get(OsceStation, i).station_number for i in second.station_ids] == [10, 11, 12]
