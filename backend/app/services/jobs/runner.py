"""Chunked, resumable background job runner.

Render's free tier offers no background workers and sleeps the web service after
15 minutes of inactivity, so long tasks (ingesting a 38-page report, generating
model answers for 18 questions, grading a paper) cannot run as one blocking
call. Instead each job advances in small chunks, persisting a `cursor` after
every chunk. If the process is killed mid-job, the next tick resumes from the
last committed cursor rather than starting over.

A single daemon thread drains the queue while the process is alive. That is
safe because the free tier runs exactly one instance; if this is ever scaled
out, claiming needs a `SELECT ... FOR UPDATE SKIP LOCKED`.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import session_scope
from app.models import Job
from app.models.ops import (
    JOB_CANCELLED,
    JOB_COMPLETED,
    JOB_FAILED,
    JOB_PENDING,
    JOB_RUNNING,
)
from app.services.errors import log_error

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
IDLE_SLEEP_SECONDS = 2.0
# A job whose heartbeat is older than this was interrupted (process restart)
# and is safe to reclaim.
STALE_AFTER_SECONDS = 300


class JobHandlerError(RuntimeError):
    """Raised by a handler to fail a job with a readable message."""


@dataclass
class JobContext:
    """Passed to a handler for one chunk of work."""

    db: Session
    job: Job

    @property
    def payload(self) -> dict[str, Any]:
        return self.job.payload or {}

    def cursor_get(self, key: str, default: Any = None) -> Any:
        return (self.job.cursor or {}).get(key, default)

    def cursor_set(self, **values: Any) -> None:
        cursor = dict(self.job.cursor or {})
        cursor.update(values)
        self.job.cursor = cursor

    def set_total(self, total: int) -> None:
        self.job.total_steps = total

    def advance(self, steps: int = 1, message: str | None = None) -> None:
        self.job.completed_steps += steps
        if message:
            self.job.message = message

    def set_message(self, message: str) -> None:
        self.job.message = message

    def set_result(self, **values: Any) -> None:
        result = dict(self.job.result or {})
        result.update(values)
        self.job.result = result

    @property
    def cancelled(self) -> bool:
        """Has an administrator cancelled this job since the chunk began?

        A chunk is one station or one sub-question, but that can still be a
        dozen paid calls. A handler that reads this between phases stops on the
        cancellation rather than finishing work nobody is waiting for. Reading
        it is one indexed query, so it belongs at phase boundaries, not inside
        a tight loop.
        """
        return _cancelled_meanwhile(self.db, self.job.id)


# handler(ctx) -> True when the job is finished, False if more chunks remain.
JobHandler = Callable[[JobContext], bool]
_HANDLERS: dict[str, JobHandler] = {}


def register_handler(job_type: str) -> Callable[[JobHandler], JobHandler]:
    def decorator(fn: JobHandler) -> JobHandler:
        _HANDLERS[job_type] = fn
        return fn

    return decorator


def create_job(
    db: Session,
    job_type: str,
    payload: dict[str, Any] | None = None,
    created_by_id: int | None = None,
    total_steps: int = 0,
    message: str | None = None,
) -> Job:
    job = Job(
        job_type=job_type,
        status=JOB_PENDING,
        payload=payload or {},
        cursor={},
        total_steps=total_steps,
        message=message,
        created_by_id=created_by_id,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    get_worker().wake()
    return job


class JobWorker:
    """Daemon thread draining the job queue."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="job-worker", daemon=True)
        self._thread.start()
        logger.info("Job worker started")

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=10)

    def wake(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                did_work = self._run_one_chunk()
            except Exception:  # noqa: BLE001 - the worker must never die
                logger.exception("Job worker loop error")
                did_work = False
            if not did_work:
                self._wake.wait(IDLE_SLEEP_SECONDS)
                self._wake.clear()

    def _run_one_chunk(self) -> bool:
        with session_scope() as db:
            job = _claim_next(db)
            if job is None:
                return False
            job_id, job_type = job.id, job.job_type

        handler = _HANDLERS.get(job_type)
        if handler is None:
            with session_scope() as db:
                failed = db.get(Job, job_id)
                if failed:
                    failed.status = JOB_FAILED
                    failed.error = f"No handler registered for job type '{job_type}'"
                    failed.finished_at = datetime.now(timezone.utc)
            return True

        with session_scope() as db:
            job = db.get(Job, job_id)
            if job is None or job.status != JOB_RUNNING:
                return True
            ctx = JobContext(db=db, job=job)
            try:
                finished = handler(ctx)
                if _cancelled_meanwhile(db, job_id):
                    # Cancelling commits from the request's session while this
                    # one holds a `job` loaded before it. Writing a status here
                    # would put the row back to PENDING and the queue would
                    # serve the job again, so the cancellation is left standing.
                    # The chunk's own work is kept - it is already paid for.
                    return True
                job.heartbeat_at = datetime.now(timezone.utc)
                if finished:
                    job.status = JOB_COMPLETED
                    job.finished_at = datetime.now(timezone.utc)
                    if job.total_steps:
                        job.completed_steps = job.total_steps
                else:
                    # Leave RUNNING so the next loop iteration picks it back up.
                    job.status = JOB_PENDING
            except Exception as exc:  # noqa: BLE001 - recorded on the job
                db.rollback()
                _fail_chunk(job_id, exc)
        return True


def _cancelled_meanwhile(db: Session, job_id: int) -> bool:
    """Read the job's status straight from the database.

    `db.get` would hand back the instance this session already holds, which was
    loaded before the cancellation was committed, so the status has to be
    selected afresh.
    """
    status = db.execute(select(Job.status).where(Job.id == job_id)).scalar_one_or_none()
    return status == JOB_CANCELLED


def _fail_chunk(job_id: int, exc: Exception) -> None:
    detail = traceback.format_exc()
    with session_scope() as db:
        job = db.get(Job, job_id)
        if job is None:
            return
        if job.status == JOB_CANCELLED:
            # Cancelling a job whose chunk is in flight often makes that chunk
            # raise. Recording it as a failure would be reporting the
            # cancellation back to the admin who asked for it.
            logger.info("Job %s chunk raised after being cancelled: %s", job_id, exc)
            return
        job.attempts += 1
        job.error = f"{type(exc).__name__}: {exc}"[:4000]
        if job.attempts >= MAX_ATTEMPTS or isinstance(exc, JobHandlerError):
            job.status = JOB_FAILED
            job.finished_at = datetime.now(timezone.utc)
        else:
            job.status = JOB_PENDING
        log_error(
            db,
            source=f"job:{job.job_type}",
            message=str(exc),
            detail=detail,
            context={"job_id": job_id, "attempts": job.attempts},
        )
    logger.error("Job %s chunk failed: %s", job_id, exc)


def _claim_next(db: Session) -> Job | None:
    """Mark the next runnable job RUNNING and return it.

    Returns None when the queue holds nothing runnable. A reclaimed job that has
    exhausted its attempts is failed here and the search continues, so one
    poisonous job does not hide the work queued behind it.
    """
    while True:
        job = _take_next(db)
        if job is None:
            return None
        if job.status != JOB_RUNNING:
            # Waiting work, not a reclaim: no attempt has been spent on it yet.
            _mark_running(job)
            return job
        # A reclaim. The previous attempt did not fail through `_fail_chunk` -
        # it took the process down with it (or the host restarted), so nothing
        # counted it. Counting it here is what stops a chunk that reliably
        # kills the worker from being retried forever.
        job.attempts += 1
        if job.attempts >= MAX_ATTEMPTS:
            job.status = JOB_FAILED
            job.finished_at = datetime.now(timezone.utc)
            job.error = (
                f"Abandoned {job.attempts} times without completing a chunk. The worker "
                "was interrupted each time rather than raising, so no error was recorded."
            )
            log_error(
                db,
                source=f"job:{job.job_type}",
                message="Job failed after repeated interruptions",
                context={"job_id": job.id, "attempts": job.attempts},
            )
            logger.error("Job %s failed after %s interruptions", job.id, job.attempts)
            continue
        logger.warning("Reclaiming stale job %s (attempt %s)", job.id, job.attempts + 1)
        _mark_running(job)
        return job


def _mark_running(job: Job) -> None:
    now = datetime.now(timezone.utc)
    job.status = JOB_RUNNING
    job.heartbeat_at = now
    if job.started_at is None:
        job.started_at = now


def _take_next(db: Session) -> Job | None:
    """The oldest job that is either waiting or abandoned, or None."""
    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(seconds=STALE_AFTER_SECONDS)

    # Waiting work and abandoned work are one queue, taken oldest first. They
    # used to be two, with pending checked before any reclaim, so a job the
    # instance died in the middle of was overtaken by everything queued after
    # it: station images stopped at 3 of 10, a 28-station batch queued later
    # jumped ahead, and the first job would not have been touched again until
    # that finished - or ever, had anything else been queued meanwhile.
    #
    # A job is reclaimed only once its heartbeat has gone quiet for
    # STALE_AFTER_SECONDS, so this cannot steal one from a live worker.
    job = db.execute(
        select(Job)
        .where(
            (Job.status == JOB_PENDING)
            | (
                (Job.status == JOB_RUNNING)
                & ((Job.heartbeat_at.is_(None)) | (Job.heartbeat_at < stale_before))
            )
        )
        .order_by(Job.id)
        .limit(1)
    ).scalar_one_or_none()
    return job


def cancel_job(db: Session, job_id: int) -> bool:
    job = db.get(Job, job_id)
    if job is None or job.status in {JOB_COMPLETED, JOB_FAILED, JOB_CANCELLED}:
        return False
    job.status = JOB_CANCELLED
    job.finished_at = datetime.now(timezone.utc)
    db.commit()
    return True


_worker = JobWorker()


def get_worker() -> JobWorker:
    return _worker


def start_worker() -> None:
    _worker.start()


def stop_worker() -> None:
    _worker.stop()
