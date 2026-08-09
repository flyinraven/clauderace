"""A candidate at the screen does not queue behind a batch.

A 461-figure re-captioning batch was running while a circuit was being sat.
All five of that sitting's transcriptions queued behind it, the station showed
"Transcribing..." for twenty minutes, and the answers were not there to mark.

Batch work has nobody watching it. A sitting does.
"""

from __future__ import annotations

from app.models import Job
from app.models.ops import JOB_PENDING
from app.services.jobs.runner import _take_next


def _job(db, job_type: str) -> Job:
    job = Job(job_type=job_type, status=JOB_PENDING, payload={}, cursor={},
              total_steps=1, completed_steps=0)
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def test_a_sittings_transcription_goes_before_a_batch(db):
    batch = _job(db, "recaption_station_figures")
    spoken = _job(db, "transcribe_response")

    nxt = _take_next(db)

    assert nxt.id == spoken.id, "the candidate is waiting; the batch is not"
    assert batch.id > 0


def test_grading_a_sitting_goes_first_too(db):
    _job(db, "source_station_images")
    grading = _job(db, "grade_osce_session")

    assert _take_next(db).id == grading.id


def test_batches_keep_their_own_order(db):
    first = _job(db, "settle_stations")
    _job(db, "recaption_station_figures")

    assert _take_next(db).id == first.id, "oldest first among work nobody waits on"


def test_two_sittings_are_served_oldest_first(db):
    first = _job(db, "transcribe_response")
    _job(db, "transcribe_response")

    assert _take_next(db).id == first.id
