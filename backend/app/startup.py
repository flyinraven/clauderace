"""Startup tasks: schema migration and admin bootstrap."""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.constants import ROLE_ADMIN
from app.models import User
from app.security import hash_password

logger = logging.getLogger(__name__)

BACKEND_ROOT = Path(__file__).resolve().parent.parent


def run_migrations() -> None:
    """Bring the database up to head on boot.

    Running migrations at startup rather than as a separate deploy step keeps
    the Render setup to a single command, which matters because the free tier
    has no pre-deploy hook.
    """
    ini_path = BACKEND_ROOT / "alembic.ini"
    if not ini_path.exists():
        logger.warning("alembic.ini not found at %s - skipping migrations", ini_path)
        return
    cfg = Config(str(ini_path))
    cfg.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
    command.upgrade(cfg, "head")
    logger.info("Database schema is up to date")


def bootstrap_admin_user(db: Session) -> None:
    """Ensure an administrator exists.

    Creates the account from BOOTSTRAP_ADMIN_EMAIL/PASSWORD if missing, and
    promotes it if it already exists as a student. The password is only applied
    at creation, so rotating it requires the change-password endpoint.
    """
    email = (settings.bootstrap_admin_email or "").strip().lower()
    if not email:
        admin_count = db.execute(
            select(func.count(User.id)).where(User.role == ROLE_ADMIN)
        ).scalar_one()
        if not admin_count:
            logger.warning(
                "No administrator exists and BOOTSTRAP_ADMIN_EMAIL is unset. "
                "Set BOOTSTRAP_ADMIN_EMAIL and BOOTSTRAP_ADMIN_PASSWORD, then restart."
            )
        return

    user = db.execute(
        select(User).where(func.lower(User.email) == email)
    ).scalar_one_or_none()

    if user is None:
        password = settings.bootstrap_admin_password
        if not password:
            logger.error(
                "BOOTSTRAP_ADMIN_EMAIL is set but BOOTSTRAP_ADMIN_PASSWORD is not; "
                "cannot create the administrator account."
            )
            return
        db.add(
            User(
                email=email,
                full_name="Administrator",
                password_hash=hash_password(password),
                role=ROLE_ADMIN,
                is_active=True,
            )
        )
        logger.info("Created bootstrap administrator %s", email)
        return

    if user.role != ROLE_ADMIN or not user.is_active:
        user.role = ROLE_ADMIN
        user.is_active = True
        logger.info("Promoted %s to administrator", email)
