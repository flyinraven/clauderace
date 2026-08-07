"""Authentication: sign in, redeem invite, change password, current user.

There is no public signup - an administrator issues an invite code, and the
holder redeems it to create their account.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession
from app.constants import ROLE_STUDENT
from app.models import Invite, User
from app.security import create_access_token, hash_password, verify_password
from app.services.throttle import client_key, invite_throttle, login_throttle

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RedeemInviteRequest(BaseModel):
    code: str = Field(min_length=4, max_length=64)
    email: EmailStr
    full_name: str | None = Field(default=None, max_length=255)
    password: str = Field(min_length=10, max_length=256)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=10, max_length=256)


class UserOut(BaseModel):
    id: int
    email: EmailStr
    full_name: str | None
    role: str
    is_active: bool

    model_config = {"from_attributes": True}


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


def _find_user_by_email(db, email: str) -> User | None:
    return db.execute(
        select(User).where(func.lower(User.email) == email.strip().lower())
    ).scalar_one_or_none()


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, request: Request, db: DbSession) -> TokenResponse:
    # Two buckets. The address one stops a script working through a password
    # list; the email one still holds when the address is forged or shared,
    # which is what protects a particular account.
    by_address = client_key(request, scope="login")
    by_email = f"login-email:{payload.email.strip().lower()}"
    login_throttle.check(by_address)
    login_throttle.check(by_email)

    user = _find_user_by_email(db, payload.email)
    # Same message either way so the endpoint cannot enumerate accounts.
    invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password"
    )
    if user is None or not verify_password(payload.password, user.password_hash):
        login_throttle.record_failure(by_address)
        login_throttle.record_failure(by_email)
        raise invalid
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="This account has been disabled"
        )

    login_throttle.clear(by_address)
    login_throttle.clear(by_email)
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()

    return TokenResponse(
        access_token=create_access_token(user.id, user.role, token_version=user.token_version),
        user=UserOut.model_validate(user),
    )


@router.post("/redeem-invite", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def redeem_invite(payload: RedeemInviteRequest, request: Request, db: DbSession) -> TokenResponse:
    bucket = client_key(request, scope="invite")
    invite_throttle.check(bucket)

    code = payload.code.strip().upper()
    invite = db.execute(select(Invite).where(Invite.code == code)).scalar_one_or_none()

    if invite is None or invite.used_by_id is not None:
        # The only failure here that is worth counting: the others mean the
        # caller holds a real code and got a detail wrong.
        invite_throttle.record_failure(bucket)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This invite code is not valid or has already been used",
        )
    if invite.expires_at and invite.expires_at < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="This invite code has expired"
        )
    if invite.email and invite.email.strip().lower() != payload.email.strip().lower():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This invite code was issued for a different email address",
        )
    if _find_user_by_email(db, payload.email) is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An account already exists for this email address",
        )

    user = User(
        email=payload.email.strip().lower(),
        full_name=(payload.full_name or "").strip() or None,
        password_hash=hash_password(payload.password),
        role=invite.role or ROLE_STUDENT,
        is_active=True,
        last_login_at=datetime.now(timezone.utc),
    )
    db.add(user)
    db.flush()

    invite_throttle.clear(bucket)
    invite.used_by_id = user.id
    invite.used_at = datetime.now(timezone.utc)
    db.commit()

    return TokenResponse(
        access_token=create_access_token(user.id, user.role, token_version=user.token_version),
        user=UserOut.model_validate(user),
    )


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(payload: ChangePasswordRequest, user: CurrentUser, db: DbSession) -> None:
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect"
        )
    user.password_hash = hash_password(payload.new_password)
    # Every token issued under the old password stops here, including the one
    # that made this call. A password is changed because it may be known, and a
    # 12-hour token that outlives it would make the change cosmetic.
    user.token_version += 1
    db.commit()
