"""Two workers on one database must not take the same job.

For a long time the runner claimed jobs with a plain SELECT, which was safe
only because exactly one instance was ever running. Then the API was moved
between hosts and the old one was left up for an afternoon against the same
database. Both workers claimed the same chunks and both paid the model for
them.

SQLite has no row locks, so the behaviour cannot be demonstrated on the test
database. What can be asserted is that the statement the runner sends to
Postgres carries the lock - which is the thing that was missing.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite

from app.models import Job
from app.models.ops import JOB_PENDING
from app.services.jobs.runner import _claim_next, _take_next


def _claim_sql() -> str:
    """The SELECT `_take_next` builds, as Postgres will receive it."""
    statements: list[str] = []

    class Recorder:
        def execute(self, statement):  # noqa: D102 - a stand-in, not an API
            statements.append(
                str(statement.compile(dialect=postgresql.dialect()))
            )

            class Empty:
                def scalar_one_or_none(self):
                    return None

            return Empty()

    _take_next(Recorder())
    assert statements, "the claim did not issue a statement"
    return statements[0].upper()


def test_claiming_a_job_locks_the_row_it_takes() -> None:
    assert "FOR UPDATE" in _claim_sql()


def test_a_worker_steps_over_a_locked_job_rather_than_blocking() -> None:
    # Without SKIP LOCKED a second worker waits on the first one's row for as
    # long as that chunk takes, instead of getting on with the next job.
    assert "SKIP LOCKED" in _claim_sql()


def test_sqlite_is_not_asked_for_a_lock_it_does_not_have() -> None:
    # The tests and the local database run on SQLite, whose dialect renders no
    # locking clause. If it ever did, every test touching the queue would fail
    # with a syntax error instead of this one line.
    rendered = str(
        select(Job).with_for_update(skip_locked=True).compile(dialect=sqlite.dialect())
    ).upper()
    assert "FOR UPDATE" not in rendered


def test_the_claim_still_returns_waiting_work(db) -> None:
    # The lock must not change which job comes back, only who else may have it.
    job = Job(job_type="source_station_images", status=JOB_PENDING, payload={},
              cursor={}, total_steps=1, completed_steps=0)
    db.add(job)
    db.commit()

    claimed = _claim_next(db)

    assert claimed is not None and claimed.id == job.id
