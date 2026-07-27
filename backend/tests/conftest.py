"""Shared fixtures for the end-to-end API tests.

The whole application is exercised against an in-memory database and a fake AI
provider. The fake sits at the HTTP boundary (`AIClient._post`) rather than
replacing `complete_json`, so the real routing, retry, JSON-repair, usage
accounting and budget code all run - those are the parts that have broken
before, and stubbing one level higher would skip them entirely.

`TestClient(app)` is used without its context manager on purpose: entering it
would run the lifespan, which migrates the real database and starts the
background worker. Jobs are instead driven a chunk at a time by `run_jobs`,
which is what lets a test assert on the state after each step.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import get_db
from app.constants import ROLE_ADMIN, ROLE_STUDENT
from app.main import app
from app.models import Base, Setting, User
from app.security import create_access_token, hash_password
from app.services.ai.client import AIClient


# --- Database -------------------------------------------------------------
@pytest.fixture()
def engine():
    # StaticPool keeps every connection pointed at the same in-memory database,
    # which the job runner's own sessions depend on.
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def sessionmaker_for(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def db(sessionmaker_for) -> Session:
    with sessionmaker_for() as session:
        yield session


@pytest.fixture(autouse=True)
def _isolate_job_sessions(monkeypatch, sessionmaker_for):
    """Point the job runner's own `session_scope` at the test database."""
    from contextlib import contextmanager

    @contextmanager
    def scope():
        session = sessionmaker_for()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    import app.services.jobs.runner as runner

    monkeypatch.setattr(runner, "session_scope", scope)
    return scope


# --- Users and client ----------------------------------------------------
TEST_PASSWORD = "correct-horse-battery"

# bcrypt is deliberately slow, and every test builds two users. Hashing the one
# shared test password once takes the suite from minutes to seconds without
# weakening what is being tested - the real `hash_password` still produced it.
_SHARED_HASH: str | None = None


def _shared_hash() -> str:
    global _SHARED_HASH
    if _SHARED_HASH is None:
        _SHARED_HASH = hash_password(TEST_PASSWORD)
    return _SHARED_HASH


def _make_user(db: Session, email: str, role: str) -> User:
    user = User(
        email=email,
        full_name=email.split("@")[0],
        password_hash=_shared_hash(),
        role=role,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture()
def admin(db: Session) -> User:
    return _make_user(db, "admin@example.com", ROLE_ADMIN)


@pytest.fixture()
def student(db: Session) -> User:
    return _make_user(db, "student@example.com", ROLE_STUDENT)


@pytest.fixture()
def client(sessionmaker_for):
    def override():
        session = sessionmaker_for()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override
    test_client = TestClient(app)
    yield test_client
    app.dependency_overrides.clear()


def auth(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id, user.role)}"}


# --- Fake AI provider ----------------------------------------------------
class FakeProvider:
    """Answers `/chat/completions` with something shaped like the real thing.

    Records every request so a test can assert on what was actually sent -
    which is how the image-size and prompt-content guarantees are checked.
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.responder = default_responder

    def post(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        self.requests.append({"url": url, "headers": headers, "body": body})
        content = self.responder(body, len(self.requests))
        return {
            "model": body.get("model", "fake/model"),
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "cost": 0.0001},
        }

    @property
    def last_body(self) -> dict[str, Any]:
        return self.requests[-1]["body"]

    def user_text(self, index: int = -1) -> str:
        """The text parts of one request's user message, concatenated."""
        content = self.requests[index]["body"]["messages"][-1]["content"]
        if isinstance(content, str):
            return content
        return "\n".join(p.get("text", "") for p in content if p.get("type") == "text")

    def images(self, index: int = -1) -> list[str]:
        """The base64 payloads of one request's images."""
        content = self.requests[index]["body"]["messages"][-1]["content"]
        if isinstance(content, str):
            return []
        return [
            p["image_url"]["url"].split(",", 1)[-1]
            for p in content
            if p.get("type") == "image_url"
        ]


def default_responder(body: dict[str, Any], call_number: int) -> str:
    """Guess from the prompt what shape of JSON the caller wants."""
    system = str(body.get("messages", [{}])[0].get("content", ""))
    user = json.dumps(body.get("messages", [])[-1].get("content", ""))

    if "marking the transcript" in system or "marking one question at a station" in system:
        return json.dumps(
            {
                "breakdown": [{"index": 0, "awarded": 1, "comment": "Named the sign."}],
                "awarded_total": 1,
                "feedback": "Described the disc well; missed the field defect.",
            }
        )
    if "marking one sub-question" in system:
        # point_id values are quoted in the prompt as "point_id=<n>"; echo the
        # first one back so the award lands on a real key point.
        import re

        ids = [int(m) for m in re.findall(r"point_id=(\d+)", user)]
        return json.dumps(
            {
                "breakdown": [
                    {"point_id": i, "awarded": 1, "comment": "Covered."} for i in ids[:2]
                ],
                "awarded_total": min(2, len(ids)),
                "feedback": "Solid, but incomplete.",
            }
        )
    if "official marking key" in system:
        import re

        ids = [int(m) for m in re.findall(r"part_id=(\d+)", user)]
        return json.dumps(
            {
                "parts": [
                    {
                        "part_id": i,
                        "key_points": [
                            {
                                "text": "Slit lamp examination",
                                "marks": 1,
                                "is_critical": True,
                                "from_examiner_feedback": True,
                                "rationale": None,
                                "accepted_alternatives": ["biomicroscopy"],
                            },
                            {
                                "text": "Intraocular pressure",
                                "marks": 1,
                                "is_critical": False,
                                "from_examiner_feedback": False,
                                "rationale": None,
                                "accepted_alternatives": [],
                            },
                        ],
                    }
                    for i in ids
                ],
                "figure_descriptions": [],
                "angoff_expected": 0.6,
                "angoff_rationale": "Mid-range difficulty.",
                "examiner_note": "Watch the arithmetic.",
            }
        )
    if "running one station of the RACE OSCE" in system:
        return json.dumps(
            {
                "prompts": [
                    {
                        "label": "A",
                        "text": "Please examine the anterior segment of the left eye.",
                        "seconds": 270,
                        "rubric": [
                            {"text": "Describes the corneal opacity", "marks": 10,
                             "is_critical": True}
                        ],
                    },
                    {
                        "label": "B",
                        "text": "What are the risk factors? Name 4.",
                        "seconds": 270,
                        "rubric": [{"text": "Names four risk factors", "marks": 10,
                                    "is_critical": False}],
                    },
                ]
            }
        )
    if "image search queries" in system:
        return json.dumps({"queries": ["slit lamp photograph corneal scar", "corneal scar",
                                       "corneal opacity"]})
    if "suitable to show a candidate" in system:
        return json.dumps(
            {
                "tier": "faithful",
                "confidence": 0.9,
                "shows": "A slit lamp photograph of a corneal scar.",
                "reason": "Right modality and sign.",
                "missing": None,
                "caption": "Slit lamp photograph, left eye",
            }
        )
    if "ELICITED" in system and "GIVEN" in system:
        return json.dumps(
            {
                "given": "Visual acuity 6/24 left, IOP 16 mmHg.",
                "elicited": "Dense central corneal opacity with neovascularisation.",
            }
        )
    return json.dumps({"ok": True})


@pytest.fixture()
def ai(monkeypatch, db: Session) -> FakeProvider:
    """Configure a provider in the settings table and intercept its HTTP calls."""
    for key, value in {
        "ai.provider": "openrouter",
        "ai.api_key": "test-key",
        "ai.model.structuring": "fake/flash",
        "ai.model.utility": "fake/flash",
        "ai.model.grading": "fake/flash",
        "ai.model.vision": "fake/flash",
        "ai.model.generation": "fake/sonnet",
        "ai.model.model_answer": "fake/sonnet",
        # A test that makes the provider fail must not spend the real backoff
        # waiting between attempts. The retry path itself is covered directly in
        # test_ai_client.py, where the sleep is asserted on rather than served.
        "ai.max_retries": 1,
        "ai.max_retry_delay_seconds": 0,
    }.items():
        db.add(Setting(key=key, value=value, is_encrypted=False))
    db.commit()

    provider = FakeProvider()
    monkeypatch.setattr(
        AIClient, "_post", lambda self, url, headers, body: provider.post(url, headers, body)
    )
    return provider


# --- Job driving ---------------------------------------------------------
@pytest.fixture()
def run_jobs(sessionmaker_for, _isolate_job_sessions):
    """Drain the job queue synchronously, one chunk at a time.

    The real worker is a thread; driving it by hand keeps tests deterministic
    and still runs the identical claim/chunk/resume code path.
    """
    from app.services.jobs.runner import get_worker

    def drain(max_chunks: int = 400) -> int:
        worker = get_worker()
        chunks = 0
        while chunks < max_chunks and worker._run_one_chunk():  # noqa: SLF001
            chunks += 1
        return chunks

    return drain
