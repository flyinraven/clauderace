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
from app.models.ops import JOB_PENDING, JOB_RUNNING
from app.services.imagesearch.base import ImageQueryError, ImageSearchError
from app.services.imagesearch.providers import tidy_query
from app.services.jobs.runner import STALE_AFTER_SECONDS, _claim_next


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
