"""Shared FastAPI dependencies."""

from __future__ import annotations

from typing import Annotated, TypeVar

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.constants import ROLE_ADMIN
from app.db import get_db
from app.models import User
from app.security import decode_access_token
from app.services.settings_store import SettingsStore

bearer_scheme = HTTPBearer(auto_error=False)

DbSession = Annotated[Session, Depends(get_db)]


def get_current_user(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> User:
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_access_token(credentials.credentials)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired or invalid. Please sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        user_id = int(payload.get("sub", ""))
    except (TypeError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Account not found or disabled"
        )
    # Retired by a password change or by the account having been disabled. A
    # token minted before `tv` existed carries none, which reads as 0 - the
    # version every account starts at, so nobody is signed out by the upgrade.
    if payload.get("tv", 0) != (user.token_version or 0):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session ended. Please sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_admin(user: CurrentUser) -> User:
    if user.role != ROLE_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Administrator access required"
        )
    return user


AdminUser = Annotated[User, Depends(require_admin)]


T = TypeVar("T")


def load_owned(
    db: Session,
    model: type[T],
    row_id: int,
    user: User,
    *,
    missing: str = "Sitting not found",
    forbidden: str = "This is not your sitting",
) -> T:
    """Fetch a row the caller owns, or refuse.

    A candidate may only reach their own sittings; an administrator may reach
    anyone's, because they review results and diagnose failed marking. Written
    papers and OSCE stations had a byte-identical copy of this each.
    """
    row = db.get(model, row_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=missing)
    if row.user_id != user.id and user.role != ROLE_ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=forbidden)
    return row


def get_settings_store(db: DbSession) -> SettingsStore:
    return SettingsStore(db)


Settings = Annotated[SettingsStore, Depends(get_settings_store)]
