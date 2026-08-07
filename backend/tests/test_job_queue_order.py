"""Which job the worker picks up next, and what a failed search costs.

Motivating failure: a station-images batch was interrupted when the free
instance spun down mid-run. It stayed marked RUNNING with a stale heartbeat,
a 28-station batch queued afterwards was picked up ahead of it, and the
interrupted one sat at "3 of 10" untouched. Then both were failed outright by
a single HTTP 500 from the search provider.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import Job
from app.models.ops import JOB_CANCELLED, JOB_FAILED, JOB_PENDING, JOB_RUNNING
from app.services.imagesearch.base import ImageQueryError, ImageSearchError
from app.services.imagesearch.providers import tidy_query
from app.services.jobs.runner import (
    MAX_ATTEMPTS,
    STALE_AFTER_SECONDS,
    _claim_next,
    cancel_job,
    register_handler,
)


def _job(db, *, status, heartbeat_minutes_ago=None):
    now = datetime.now(timezone.utc)
    job = Job(
        job_type="source_station_images",
        status=status,
        payload={"station_ids": [1]},
        cursor={},
        total_steps=1,
        heartbeat_at=(
            now - timedelta(minutes=heartbeat_minutes_ago)
            if heartbeat_minutes_ago is not None else None
        ),
    )
    db.add(job)
    db.commit()
    return job


def test_an_interrupted_job_is_resumed_before_a_newer_one_is_started(db):
    """Otherwise a batch is overtaken indefinitely by whatever is queued after it."""
    interrupted = _job(db, status=JOB_RUNNING, heartbeat_minutes_ago=30)
    queued_later = _job(db, status=JOB_PENDING)

    claimed = _claim_next(db)
    assert claimed.id == interrupted.id, (
        "the abandoned job is older and must be finished first"
    )
    assert claimed.id != queued_later.id


def test_a_job_a_live_worker_is_running_is_never_stolen(db):
    """A fresh heartbeat means someone is on it."""
    _job(db, status=JOB_RUNNING, heartbeat_minutes_ago=0)
    assert _claim_next(db) is None

    # And only once it has gone quiet for long enough.
    _job(db, status=JOB_RUNNING,
         heartbeat_minutes_ago=(STALE_AFTER_SECONDS / 60) + 1)
    assert _claim_next(db) is not None


def test_queued_jobs_still_run_oldest_first(db):
    first = _job(db, status=JOB_PENDING)
    _job(db, status=JOB_PENDING)
    assert _claim_next(db).id == first.id


def test_a_reclaim_spends_an_attempt(db):
    """A crash records nothing, so the reclaim is the only place to count it."""
    job = _job(db, status=JOB_RUNNING, heartbeat_minutes_ago=30)
    assert job.attempts == 0
    assert _claim_next(db).id == job.id
    assert job.attempts == 1


def test_a_job_that_keeps_killing_the_worker_eventually_fails(db):
    """Otherwise a chunk that takes the process down is retried forever.

    Nothing raises here - the job is simply found abandoned each time, which is
    what a process killed mid-chunk looks like from the next tick.
    """
    job = _job(db, status=JOB_RUNNING, heartbeat_minutes_ago=30)
    for _ in range(MAX_ATTEMPTS - 1):
        assert _claim_next(db) is not None
        job.heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        db.commit()

    assert _claim_next(db) is None, "the queue is empty now, not serving it again"
    assert job.status == JOB_FAILED
    assert job.attempts == MAX_ATTEMPTS
    assert "interrupted" in (job.error or "")


def test_a_poisonous_job_does_not_hide_the_work_behind_it(db):
    """It is failed and the queue moves on within the same claim."""
    doomed = _job(db, status=JOB_RUNNING, heartbeat_minutes_ago=30)
    doomed.attempts = MAX_ATTEMPTS - 1
    db.commit()
    waiting = _job(db, status=JOB_PENDING)

    claimed = _claim_next(db)
    assert claimed is not None and claimed.id == waiting.id
    assert doomed.status == JOB_FAILED


def test_cancelling_a_job_mid_chunk_is_not_undone_by_the_chunk_finishing(
    db, sessionmaker_for, run_jobs
):
    """The worker holds a `job` loaded before the cancellation committed.

    Writing any status from that stale object - PENDING for "more to do" -
    put the row straight back in the queue, so cancelling a 28-station batch
    only worked if the click landed between two chunks. It kept spending.
    """
    chunks_run = []

    @register_handler("test_cancel_midway")
    def _handler(ctx):
        chunks_run.append(ctx.job.id)
        if len(chunks_run) == 1:
            # Stands in for the administrator pressing cancel while this chunk
            # is out at the provider: a different session, committed underneath.
            other = sessionmaker_for()
            try:
                cancel_job(other, ctx.job.id)
            finally:
                other.close()
        return False  # "more chunks to come" - the status the fix must not write

    job = Job(job_type="test_cancel_midway", status=JOB_PENDING, payload={}, cursor={})
    db.add(job)
    db.commit()

    run_jobs()
    db.expire_all()

    assert len(chunks_run) == 1, "the cancelled job must not be handed out again"
    assert db.get(Job, job.id).status == JOB_CANCELLED


def test_a_chunk_that_raises_after_a_cancel_is_not_reported_as_a_failure(
    db, sessionmaker_for, run_jobs
):
    """Cancelling mid-flight usually makes the chunk raise. That is the
    cancellation arriving, not a job that went wrong."""

    @register_handler("test_cancel_then_raise")
    def _handler(ctx):
        other = sessionmaker_for()
        try:
            cancel_job(other, ctx.job.id)
        finally:
            other.close()
        raise RuntimeError("connection closed")

    job = Job(job_type="test_cancel_then_raise", status=JOB_PENDING, payload={}, cursor={})
    db.add(job)
    db.commit()

    run_jobs()
    db.expire_all()

    stored = db.get(Job, job.id)
    assert stored.status == JOB_CANCELLED
    assert stored.attempts == 0
    assert stored.error is None


@pytest.mark.parametrize(
    "raw, expected",
    [
        # The exact phrase Brave answered HTTP 500 to, full stop and all.
        (
            "nine positions of gaze photograph Congenital fibrosis of extraocular muscles.",
            "nine positions of gaze photograph Congenital fibrosis of extraocular muscles",
        ),
        ("OCT macula (mild CME left eye)", "OCT macula"),
        ("macular ectopia (right > left)", "macular ectopia"),
    ],
)
def test_a_query_is_tidied_before_it_is_sent(raw, expected):
    assert tidy_query(raw) == expected


def test_a_very_long_query_is_cut_at_a_word(): 
    query = "nine positions of gaze photograph " + " ".join(["retinopathy"] * 40)
    tidied = tidy_query(query)
    assert len(tidied) <= 160
    assert not tidied.endswith("retinopath"), "cut between words, not through one"


def test_a_failed_query_is_not_an_exhausted_account():
    """One is worth trying the next phrase for; the other stops the run.

    Subclassing matters: every existing `except ImageSearchError` still fires.
    """
    assert issubclass(ImageQueryError, ImageSearchError)
