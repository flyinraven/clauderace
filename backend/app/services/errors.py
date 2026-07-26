"""Persistent error log surfaced in the admin portal.

Writing to the log must never be the reason a request fails, so every failure
path here is swallowed after being logged to stderr.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import ErrorLog

logger = logging.getLogger(__name__)

MAX_RETAINED_ENTRIES = 2000


def log_error(
    db: Session,
    source: str,
    message: str,
    detail: str | None = None,
    context: dict[str, Any] | None = None,
    level: str = "error",
    user_id: int | None = None,
) -> None:
    try:
        db.add(
            ErrorLog(
                created_at=datetime.now(timezone.utc),
                level=level,
                source=source[:80],
                message=str(message)[:4000],
                detail=detail[:20000] if detail else None,
                context=context,
                user_id=user_id,
            )
        )
        db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("Failed to write error log entry")
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def prune_error_log(db: Session, keep: int = MAX_RETAINED_ENTRIES) -> int:
    """Trim the log so a runaway loop cannot fill the SiteGround disk."""
    try:
        cutoff = db.execute(
            select(ErrorLog.id).order_by(ErrorLog.id.desc()).offset(keep).limit(1)
        ).scalar_one_or_none()
        if cutoff is None:
            return 0
        result = db.execute(delete(ErrorLog).where(ErrorLog.id <= cutoff))
        db.commit()
        return result.rowcount or 0
    except Exception:  # noqa: BLE001
        logger.exception("Failed to prune error log")
        db.rollback()
        return 0
