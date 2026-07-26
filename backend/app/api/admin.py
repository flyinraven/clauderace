"""Administrator portal API: settings, users, invites, jobs, error log."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select

from app.api.deps import AdminUser, DbSession
from app.constants import ROLE_ADMIN, ROLE_STUDENT
from app.models import AiCall, ErrorLog, Invite, Job, Question, SourceDocument, User
from app.models.ops import JOB_PENDING, JOB_RUNNING
from app.security import generate_invite_code, hash_password
from app.services.ai import AIClient, AIError
from app.services.errors import prune_error_log
from app.services.jobs.runner import cancel_job
from app.services.settings_store import SettingsStore

router = APIRouter(prefix="/admin", tags=["admin"])


# --- Settings -------------------------------------------------------------
class SettingUpdate(BaseModel):
    key: str
    value: Any


class SettingsUpdateRequest(BaseModel):
    settings: list[SettingUpdate]


@router.get("/settings")
def list_settings(admin: AdminUser, db: DbSession) -> dict[str, Any]:
    store = SettingsStore(db)
    return {"settings": store.describe_all()}


@router.put("/settings")
def update_settings(
    payload: SettingsUpdateRequest, admin: AdminUser, db: DbSession
) -> dict[str, Any]:
    store = SettingsStore(db)
    for item in payload.settings:
        # A masked secret means "unchanged" - never overwrite a real key with
        # the asterisks the UI displayed.
        if isinstance(item.value, str) and set(item.value) == {"*"}:
            continue
        store.set(item.key, item.value, updated_by_id=admin.id)
    db.commit()
    return {"settings": SettingsStore(db).describe_all()}


class AiTestRequest(BaseModel):
    task: str = "structuring"
    prompt: str = "Reply with the single word: ready"


@router.post("/settings/test-ai")
def test_ai(payload: AiTestRequest, admin: AdminUser, db: DbSession) -> dict[str, Any]:
    """Round-trip the configured provider so misconfiguration surfaces here
    rather than halfway through a 40-minute ingestion job."""
    client = AIClient(db)
    if not client.is_configured_for(payload.task):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"The provider routed to '{payload.task}' has no API key set.",
        )
    try:
        # routing_task borrows that task's provider and model, but the call is
        # logged as a connection test rather than polluting the task's ledger.
        response = client.complete(
            task="connection_test",
            system="You are a connection test. Answer in as few words as possible.",
            user=payload.prompt,
            routing_task=payload.task,
            max_tokens=64,
        )
    except AIError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return {
        "ok": True,
        "provider": response.provider,
        "slot": client.slot_for(payload.task),
        "model": response.model,
        "reply": response.text.strip(),
        "latency_ms": response.latency_ms,
        "prompt_tokens": response.prompt_tokens,
        "completion_tokens": response.completion_tokens,
    }


@router.get("/settings/ai-routing")
def ai_routing(admin: AdminUser, db: DbSession) -> dict[str, Any]:
    """Which provider and model currently serves each task."""
    return {"routing": AIClient(db).describe_routing()}


# --- Users ----------------------------------------------------------------
class AdminUserOut(BaseModel):
    id: int
    email: EmailStr
    full_name: str | None
    role: str
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None

    model_config = {"from_attributes": True}


class UserUpdateRequest(BaseModel):
    role: str | None = None
    is_active: bool | None = None
    full_name: str | None = None


class CreateUserRequest(BaseModel):
    email: EmailStr
    full_name: str | None = None
    password: str = Field(min_length=10, max_length=256)
    role: str = ROLE_STUDENT


@router.get("/users", response_model=list[AdminUserOut])
def list_users(admin: AdminUser, db: DbSession) -> list[AdminUserOut]:
    users = db.execute(select(User).order_by(User.created_at.desc())).scalars().all()
    return [AdminUserOut.model_validate(u) for u in users]


@router.post("/users", response_model=AdminUserOut, status_code=status.HTTP_201_CREATED)
def create_user(payload: CreateUserRequest, admin: AdminUser, db: DbSession) -> AdminUserOut:
    if payload.role not in {ROLE_STUDENT, ROLE_ADMIN}:
        raise HTTPException(status_code=400, detail="Role must be 'student' or 'admin'")
    existing = db.execute(
        select(User).where(func.lower(User.email) == payload.email.lower())
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="An account with that email already exists")
    user = User(
        email=payload.email.lower(),
        full_name=payload.full_name,
        password_hash=hash_password(payload.password),
        role=payload.role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return AdminUserOut.model_validate(user)


@router.patch("/users/{user_id}", response_model=AdminUserOut)
def update_user(
    user_id: int, payload: UserUpdateRequest, admin: AdminUser, db: DbSession
) -> AdminUserOut:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if payload.role is not None:
        if payload.role not in {ROLE_STUDENT, ROLE_ADMIN}:
            raise HTTPException(status_code=400, detail="Role must be 'student' or 'admin'")
        # Guard against locking everyone out of the admin portal.
        if user.id == admin.id and payload.role != ROLE_ADMIN:
            raise HTTPException(status_code=400, detail="You cannot remove your own admin role")
        user.role = payload.role
    if payload.is_active is not None:
        if user.id == admin.id and not payload.is_active:
            raise HTTPException(status_code=400, detail="You cannot disable your own account")
        user.is_active = payload.is_active
    if payload.full_name is not None:
        user.full_name = payload.full_name
    db.commit()
    db.refresh(user)
    return AdminUserOut.model_validate(user)


# --- Invites --------------------------------------------------------------
class InviteOut(BaseModel):
    id: int
    code: str
    email: str | None
    role: str
    note: str | None
    expires_at: datetime | None
    used_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class CreateInviteRequest(BaseModel):
    email: EmailStr | None = None
    role: str = ROLE_STUDENT
    note: str | None = None
    expires_in_days: int = 30


@router.get("/invites", response_model=list[InviteOut])
def list_invites(admin: AdminUser, db: DbSession) -> list[InviteOut]:
    invites = db.execute(select(Invite).order_by(Invite.created_at.desc())).scalars().all()
    return [InviteOut.model_validate(i) for i in invites]


@router.post("/invites", response_model=InviteOut, status_code=status.HTTP_201_CREATED)
def create_invite(payload: CreateInviteRequest, admin: AdminUser, db: DbSession) -> InviteOut:
    if payload.role not in {ROLE_STUDENT, ROLE_ADMIN}:
        raise HTTPException(status_code=400, detail="Role must be 'student' or 'admin'")
    invite = Invite(
        code=generate_invite_code(),
        email=payload.email.lower() if payload.email else None,
        role=payload.role,
        note=payload.note,
        expires_at=datetime.now(timezone.utc) + timedelta(days=max(1, payload.expires_in_days)),
        created_by_id=admin.id,
    )
    db.add(invite)
    db.commit()
    db.refresh(invite)
    return InviteOut.model_validate(invite)


@router.delete("/invites/{invite_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_invite(invite_id: int, admin: AdminUser, db: DbSession) -> None:
    invite = db.get(Invite, invite_id)
    if invite is None:
        raise HTTPException(status_code=404, detail="Invite not found")
    if invite.used_by_id is not None:
        raise HTTPException(status_code=400, detail="Cannot delete an invite that has been used")
    db.delete(invite)
    db.commit()


# --- Jobs -----------------------------------------------------------------
class JobOut(BaseModel):
    id: int
    job_type: str
    status: str
    total_steps: int
    completed_steps: int
    message: str | None
    error: str | None
    result: dict[str, Any] | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    model_config = {"from_attributes": True}


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(
    admin: AdminUser,
    db: DbSession,
    limit: int = Query(default=50, le=200),
    job_type: str | None = None,
) -> list[JobOut]:
    stmt = select(Job).order_by(Job.id.desc()).limit(limit)
    if job_type:
        stmt = stmt.where(Job.job_type == job_type)
    return [JobOut.model_validate(j) for j in db.execute(stmt).scalars().all()]


@router.post("/jobs/{job_id}/cancel")
def cancel(job_id: int, admin: AdminUser, db: DbSession) -> dict[str, bool]:
    return {"cancelled": cancel_job(db, job_id)}


# --- Error log ------------------------------------------------------------
class ErrorLogOut(BaseModel):
    id: int
    created_at: datetime
    level: str
    source: str
    message: str
    detail: str | None
    context: dict[str, Any] | None

    model_config = {"from_attributes": True}


@router.get("/errors", response_model=list[ErrorLogOut])
def list_errors(
    admin: AdminUser,
    db: DbSession,
    limit: int = Query(default=100, le=500),
    level: str | None = None,
    source: str | None = None,
) -> list[ErrorLogOut]:
    stmt = select(ErrorLog).order_by(ErrorLog.id.desc()).limit(limit)
    if level:
        stmt = stmt.where(ErrorLog.level == level)
    if source:
        stmt = stmt.where(ErrorLog.source.like(f"%{source}%"))
    return [ErrorLogOut.model_validate(e) for e in db.execute(stmt).scalars().all()]


@router.delete("/errors")
def clear_errors(admin: AdminUser, db: DbSession, keep: int = Query(default=0, le=2000)) -> dict:
    return {"deleted": prune_error_log(db, keep=keep)}


# --- Dashboard ------------------------------------------------------------
@router.get("/stats")
def stats(admin: AdminUser, db: DbSession) -> dict[str, Any]:
    def count(stmt) -> int:
        return int(db.execute(stmt).scalar_one() or 0)

    by_type = dict(
        db.execute(
            select(Question.question_type, func.count(Question.id)).group_by(Question.question_type)
        ).all()
    )
    by_subspecialty = dict(
        db.execute(
            select(Question.subspecialty, func.count(Question.id))
            .where(Question.subspecialty.is_not(None))
            .group_by(Question.subspecialty)
        ).all()
    )
    by_status = dict(
        db.execute(
            select(Question.status, func.count(Question.id)).group_by(Question.status)
        ).all()
    )

    since = datetime.now(timezone.utc) - timedelta(days=30)
    ai_rows = db.execute(
        select(
            func.count(AiCall.id),
            func.sum(AiCall.prompt_tokens),
            func.sum(AiCall.completion_tokens),
            func.sum(AiCall.cost_usd),
        ).where(AiCall.created_at >= since)
    ).one()

    return {
        "users": count(select(func.count(User.id))),
        "documents": count(select(func.count(SourceDocument.id))),
        "questions_total": count(select(func.count(Question.id))),
        "questions_by_type": by_type,
        "questions_by_subspecialty": by_subspecialty,
        "questions_by_status": by_status,
        "with_model_answers": count(
            select(func.count(Question.id)).where(Question.model_answer_status == "complete")
        ),
        "active_jobs": count(
            select(func.count(Job.id)).where(Job.status.in_([JOB_PENDING, JOB_RUNNING]))
        ),
        "errors_24h": count(
            select(func.count(ErrorLog.id)).where(
                ErrorLog.created_at >= datetime.now(timezone.utc) - timedelta(days=1)
            )
        ),
        "ai_last_30_days": {
            "calls": int(ai_rows[0] or 0),
            "prompt_tokens": int(ai_rows[1] or 0),
            "completion_tokens": int(ai_rows[2] or 0),
            "cost_usd": float(ai_rows[3] or 0.0),
        },
        "ai_budget": AIClient(db).budget_status(),
    }


@router.get("/spend")
def spend_breakdown(admin: AdminUser, db: DbSession, days: int = 30) -> dict[str, Any]:
    """Where the money went, by task and model."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = db.execute(
        select(
            AiCall.task,
            AiCall.model,
            func.count(AiCall.id),
            func.sum(AiCall.cost_usd),
            func.avg(AiCall.latency_ms),
        )
        .where(AiCall.created_at >= since)
        .where(AiCall.status == "success")
        .group_by(AiCall.task, AiCall.model)
        .order_by(func.sum(AiCall.cost_usd).desc())
    ).all()
    return {
        "days": days,
        "budget": AIClient(db).budget_status(),
        "rows": [
            {
                "task": task,
                "model": model,
                "calls": int(calls or 0),
                "cost_usd": round(float(cost or 0.0), 4),
                "avg_latency_ms": int(latency or 0),
            }
            for task, model, calls, cost, latency in rows
        ],
    }
