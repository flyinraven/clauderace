"""Provider-agnostic AI gateway with per-task routing.

Two provider slots are configurable. Each task (structuring, model answers,
generation, grading, vision) picks a slot and a model, so you can run Gemini
directly against Google AI Studio while Claude comes through OpenRouter, each
billed on its own key.

OpenRouter, OpenAI, Google AI Studio and any OpenAI-compatible endpoint share
the `/chat/completions` request shape; Anthropic's native API differs enough to
warrant its own adapter.

Every call is recorded in `ai_calls` with tokens, latency and cost.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AiCall
from app.services.ai.images import normalise_for_vision
from app.services.errors import log_error
from app.services.settings_store import SettingsStore

logger = logging.getLogger(__name__)

PROVIDER_DEFAULT_BASE_URLS = {
    "openrouter": "https://openrouter.ai/api/v1",
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    # Google AI Studio's OpenAI compatibility layer. Note the /openai suffix -
    # the older /v1beta/chat/completions path was retired.
    "google": "https://generativelanguage.googleapis.com/v1beta/openai",
}

ANTHROPIC_VERSION = "2023-06-01"

PRIMARY = "primary"
SECONDARY = "secondary"

# Tasks that can be routed independently.
AI_TASKS = ("structuring", "model_answer", "generation", "utility", "grading", "vision")


class AIError(RuntimeError):
    """Raised when a model call cannot be completed."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        # Seconds the provider asked us to wait, when it said so.
        self.retry_after = retry_after


@dataclass
class TextPart:
    text: str


@dataclass
class ImagePart:
    """One image bound for a vision model.

    The bytes are bounded to what a provider actually looks at as soon as the
    part is constructed - see `app.services.ai.images`. Doing it here rather
    than at each call site means a new vision caller cannot forget to, and
    `data` is what will really be sent, which is what the size checks want.
    """

    data: bytes
    media_type: str = "image/png"

    def __post_init__(self) -> None:
        self.data, self.media_type = normalise_for_vision(self.data, self.media_type)

    @property
    def b64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")


ContentPart = TextPart | ImagePart


@dataclass(frozen=True)
class ProviderConfig:
    slot: str
    kind: str
    base_url: str
    api_key: str

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key and self.base_url)

    @property
    def uses_anthropic_api(self) -> bool:
        return self.kind == "anthropic"

    @property
    def label(self) -> str:
        return f"{self.slot}:{self.kind}"


@dataclass
class AIResponse:
    text: str
    model: str
    provider: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: float | None = None
    latency_ms: int | None = None

    def json(self) -> Any:
        return parse_json_response(self.text)


def parse_json_response(text: str) -> Any:
    """Best-effort JSON extraction from a model response.

    Models wrap JSON in ```json fences or add a sentence of preamble often
    enough that a bare `json.loads` is unreliable, so fall back to locating the
    outermost balanced object or array.
    """
    if text is None:
        raise AIError("Empty response from model")
    candidate = text.strip()

    fence = re.search(r"```(?:json)?\s*(.*?)```", candidate, re.DOTALL)
    if fence:
        candidate = fence.group(1).strip()

    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    for opener, closer in (("{", "}"), ("[", "]")):
        start = candidate.find(opener)
        if start == -1:
            continue
        depth = 0
        in_string = False
        escape = False
        for idx in range(start, len(candidate)):
            ch = candidate[idx]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(candidate[start : idx + 1])
                    except json.JSONDecodeError:
                        break
    raise AIError(f"Model did not return valid JSON. First 500 chars: {text[:500]}")


class AIClient:
    """Reads its configuration from the settings table at construction."""

    def __init__(self, db: Session, store: SettingsStore | None = None):
        self.db = db
        self.store = store or SettingsStore(db)
        self.timeout = self.store.get_int("ai.timeout_seconds", 180)
        self.max_retries = self.store.get_int("ai.max_retries", 3)
        self.max_retry_delay = self.store.get_int("ai.max_retry_delay_seconds", 75)
        self.default_temperature = self.store.get_float("ai.temperature", 0.3)
        self.default_max_tokens = self.store.get_int("ai.max_tokens", 8000)

        self.providers = {
            PRIMARY: self._read_provider(PRIMARY, "ai.provider", "ai.base_url", "ai.api_key"),
            SECONDARY: self._read_provider(
                SECONDARY, "ai.provider2", "ai.base_url2", "ai.api_key2"
            ),
        }

    def _read_provider(self, slot: str, kind_key: str, url_key: str, key_key: str) -> ProviderConfig:
        kind = self.store.get_str(kind_key, "").lower() or "none"
        base_url = (
            self.store.get_str(url_key, "").rstrip("/")
            or PROVIDER_DEFAULT_BASE_URLS.get(kind, "")
        )
        return ProviderConfig(
            slot=slot, kind=kind, base_url=base_url, api_key=self.store.get_str(key_key, "")
        )

    # --- Routing ----------------------------------------------------------
    def slot_for(self, task: str) -> str:
        slot = self.store.get_str(f"ai.model.{task}.slot", PRIMARY).lower()
        return SECONDARY if slot == SECONDARY else PRIMARY

    def provider_for(self, task: str) -> ProviderConfig:
        """The provider serving a task.

        Deliberately does NOT fall back to the other slot when unconfigured.
        Model ids are provider-specific ("gemini-2.5-flash" on Google AI Studio
        versus "google/gemini-2.5-flash" on OpenRouter), so a silent fallback
        would send an unrecognised model id and produce a baffling 404 instead
        of the real problem, which is a missing key.
        """
        return self.providers[self.slot_for(task)]

    @property
    def is_configured(self) -> bool:
        return any(p.is_configured for p in self.providers.values())

    def is_configured_for(self, task: str) -> bool:
        return self.provider_for(task).is_configured

    def model_for(self, task: str) -> str:
        model = self.store.get_str(f"ai.model.{task}", "")
        if not model:
            model = self.store.get_str("ai.model.structuring", "")
        if not model:
            raise AIError(f"No model configured for task '{task}'")
        return model

    def describe_routing(self) -> list[dict[str, Any]]:
        """Admin-facing view of which provider serves which task."""
        out = []
        for task in AI_TASKS:
            config = self.providers[self.slot_for(task)]
            out.append(
                {
                    "task": task,
                    "slot": self.slot_for(task),
                    "provider": config.kind,
                    "base_url": config.base_url,
                    "model": self.store.get_str(f"ai.model.{task}", ""),
                    "configured": config.is_configured,
                }
            )
        return out

    # --- Public API -------------------------------------------------------
    def complete(
        self,
        task: str,
        system: str,
        user: str | list[ContentPart],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        force_json: bool = False,
        job_id: int | None = None,
        routing_task: str | None = None,
    ) -> AIResponse:
        # `routing_task` lets ad-hoc calls (e.g. a connection test) borrow
        # another task's provider and model without being logged under it.
        config = self.provider_for(routing_task or task)
        if not config.is_configured:
            raise AIError(
                f"Task '{routing_task or task}' is routed to the {config.slot} "
                f"provider ({config.kind}), which has no API key set. Configure it "
                f"in Admin > Settings, or point the task at the other provider."
            )
        self._check_budget()
        model = model or self.model_for(routing_task or task)
        temperature = self.default_temperature if temperature is None else temperature
        max_tokens = max_tokens or self.default_max_tokens
        parts: list[ContentPart] = [TextPart(user)] if isinstance(user, str) else user

        started = time.perf_counter()
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                if config.uses_anthropic_api:
                    response = self._call_anthropic(
                        config, model, system, parts, temperature, max_tokens
                    )
                else:
                    response = self._call_openai_compatible(
                        config, model, system, parts, temperature, max_tokens, force_json
                    )
                response.latency_ms = int((time.perf_counter() - started) * 1000)
                self._log_call(task, response, "success", None, job_id)
                return response
            except AIError as exc:
                last_error = exc
                if not _is_retryable(exc) or attempt == self.max_retries:
                    break
                # Honour the provider's own backoff when it gives one; a
                # per-minute quota is not cleared by a 2-second wait.
                delay = exc.retry_after if exc.retry_after else min(2 ** attempt, 15)
                time.sleep(min(delay, self.max_retry_delay))
            except Exception as exc:  # noqa: BLE001 - re-raised as AIError below
                last_error = exc
                if attempt == self.max_retries:
                    break
                time.sleep(min(2 ** attempt, 15))

        latency = int((time.perf_counter() - started) * 1000)
        message = str(last_error) if last_error else "unknown error"
        self._log_call(
            task,
            AIResponse(text="", model=model, provider=config.kind, latency_ms=latency),
            "error",
            message,
            job_id,
        )
        raise AIError(f"{task}: {message}") from last_error

    def complete_json(
        self,
        task: str,
        system: str,
        user: str | list[ContentPart],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        job_id: int | None = None,
        repair_attempts: int = 1,
        routing_task: str | None = None,
    ) -> Any:
        """Complete and parse JSON, retrying once with an explicit repair prompt."""
        response = self.complete(
            task, system, user,
            model=model, temperature=temperature, max_tokens=max_tokens,
            force_json=True, job_id=job_id, routing_task=routing_task,
        )
        try:
            return response.json()
        except AIError:
            if repair_attempts <= 0:
                raise
            # The repair carries the ORIGINAL request with it. Sending only the
            # broken reply used to drop the source material entirely, and the
            # model - still holding a system prompt telling it to write an OSCE
            # station - invented one from nothing. A station arrived with a
            # confidently stated diagnosis belonging to no patient in the bank,
            # which is far worse than the parse failure it was papering over.
            note = (
                "\n\n---\nYour previous reply to this request was not valid JSON. "
                "Answer the request above again, as a single valid JSON value and "
                "nothing else - no prose, no code fences. Do not invent content: "
                "everything must come from the request.\n\nPrevious reply:\n"
                + response.text[:6000]
            )
            repair: str | list[ContentPart] = (
                user + note if isinstance(user, str) else [*user, TextPart(note)]
            )
            repaired = self.complete(
                task, system, repair,
                model=model, temperature=0, max_tokens=max_tokens,
                force_json=True, job_id=job_id, routing_task=routing_task,
            )
            return repaired.json()

    # --- Adapters ---------------------------------------------------------
    def _call_openai_compatible(
        self,
        config: ProviderConfig,
        model: str,
        system: str,
        parts: list[ContentPart],
        temperature: float,
        max_tokens: int,
        force_json: bool,
    ) -> AIResponse:
        content: list[dict[str, Any]] = []
        for part in parts:
            if isinstance(part, TextPart):
                content.append({"type": "text", "text": part.text})
            else:
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{part.media_type};base64,{part.b64}"},
                    }
                )

        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if force_json:
            body["response_format"] = {"type": "json_object"}

        # Gemini 2.5 models think before answering, and those thinking tokens
        # come out of max_tokens. Left unconstrained a short request can burn
        # the whole budget internally and return empty content, so the thinking
        # effort is capped explicitly. "none" suits transcription and marking
        # against an explicit key; raise it for open-ended reasoning.
        if config.kind == "google":
            effort = self.store.get_str("ai.google_reasoning_effort", "none").lower()
            if effort and effort != "default":
                body["reasoning_effort"] = effort

        # OpenRouter serves the same model from several hosts at very different
        # prices - one model was $0.10/$0.60 per million from its first-party
        # host and $1.00/$6.00 from a reseller, ten times over. Left to route
        # itself, a batch can silently cost ten times what was budgeted, so the
        # host is named and fallbacks are off by default: a failure that says so
        # is better than an invoice that does not.
        if config.kind == "openrouter":
            order = [
                p.strip()
                for p in self.store.get_str("ai.openrouter_provider_order", "").split(",")
                if p.strip()
            ]
            if order:
                body["provider"] = {
                    "order": order,
                    "allow_fallbacks": self.store.get_bool(
                        "ai.openrouter_allow_fallbacks", False
                    ),
                }

        headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        }
        if config.kind == "openrouter":
            # OpenRouter attributes usage to these; harmless elsewhere.
            headers["HTTP-Referer"] = "https://exam.txglobal.com.au"
            headers["X-Title"] = "RANZCO RACE Exam Simulator"

        data = self._post(f"{config.base_url}/chat/completions", headers, body)

        try:
            choice = data["choices"][0]
            text = choice.get("message", {}).get("content") or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise AIError(f"Unexpected response shape: {json.dumps(data)[:500]}") from exc

        reason = (choice or {}).get("finish_reason")

        if not text.strip():
            if reason == "length":
                raise AIError(
                    "Model returned no content and stopped on the token limit. For "
                    "Gemini 2.5 this usually means thinking consumed the whole "
                    "budget - raise 'Max output tokens' or lower "
                    "'Google reasoning effort' in Admin > Settings."
                )
            raise AIError(f"Model returned empty content (finish_reason={reason!r}).")

        # A response cut off at the limit is almost always unparseable JSON.
        # Saying so beats letting it surface as "not a JSON object", which
        # sends you looking for a prompt problem that is not there.
        if reason == "length" and force_json:
            raise AIError(
                f"Model hit the {max_tokens}-token output limit before finishing, "
                f"so its JSON is truncated. Raise 'Max output tokens' in "
                f"Admin > Settings - long questions with many sub-questions need "
                f"more room."
            )

        usage = data.get("usage") or {}
        return AIResponse(
            text=text,
            model=data.get("model", model),
            provider=config.kind,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            cost_usd=(usage.get("cost") if isinstance(usage.get("cost"), (int, float)) else None),
        )

    def _call_anthropic(
        self,
        config: ProviderConfig,
        model: str,
        system: str,
        parts: list[ContentPart],
        temperature: float,
        max_tokens: int,
    ) -> AIResponse:
        content: list[dict[str, Any]] = []
        for part in parts:
            if isinstance(part, TextPart):
                content.append({"type": "text", "text": part.text})
            else:
                content.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": part.media_type,
                            "data": part.b64,
                        },
                    }
                )

        body = {
            "model": model,
            "system": system,
            "messages": [{"role": "user", "content": content}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        headers = {
            "x-api-key": config.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }

        data = self._post(f"{config.base_url}/messages", headers, body)

        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        usage = data.get("usage") or {}
        return AIResponse(
            text=text,
            model=data.get("model", model),
            provider=config.kind,
            prompt_tokens=usage.get("input_tokens"),
            completion_tokens=usage.get("output_tokens"),
        )

    def _post(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise AIError(f"timeout after {self.timeout}s") from exc
        except httpx.HTTPError as exc:
            raise AIError(f"network error: {exc}") from exc

        if resp.status_code >= 400:
            detail = resp.text[:800]
            retry_after = None
            if resp.status_code == 429:
                # Per-minute quotas need a real pause, not an exponential
                # backoff starting at two seconds.
                header = resp.headers.get("retry-after")
                try:
                    retry_after = float(header) if header else None
                except (TypeError, ValueError):
                    retry_after = None
                if retry_after is None:
                    match = re.search(r"retry[ -]?after[\"']?\s*[:=]\s*([0-9.]+)", detail, re.I)
                    retry_after = float(match.group(1)) if match else 45.0
            raise AIError(f"HTTP {resp.status_code}: {detail}", retry_after=retry_after)
        try:
            return resp.json()
        except ValueError as exc:
            raise AIError(f"non-JSON response: {resp.text[:300]}") from exc

    # --- Budget -----------------------------------------------------------
    def spend_this_month(self) -> float:
        """Recorded AI spend for the current calendar month, in USD."""
        from sqlalchemy import func, select

        now = datetime.now(timezone.utc)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        total = self.db.execute(
            select(func.sum(AiCall.cost_usd)).where(AiCall.created_at >= start)
        ).scalar_one_or_none()
        return float(total or 0.0)

    def budget_status(self) -> dict[str, Any]:
        budget = self.store.get_float("ai.monthly_budget_usd", 0.0)
        spent = self.spend_this_month()
        return {
            "budget_usd": budget,
            "spent_usd": round(spent, 4),
            "remaining_usd": round(max(0.0, budget - spent), 4) if budget > 0 else None,
            "enforced": budget > 0,
        }

    def _check_budget(self) -> None:
        """Refuse calls once the monthly ceiling is reached.

        Only spend the provider actually reports is counted, so this is a floor
        rather than an exact figure - it will stop a runaway batch, but it is
        not a substitute for a spending limit set at the provider.
        """
        budget = self.store.get_float("ai.monthly_budget_usd", 0.0)
        if budget <= 0:
            return
        spent = self.spend_this_month()
        if spent >= budget:
            raise AIError(
                f"Monthly AI budget of USD {budget:.2f} has been reached "
                f"(USD {spent:.2f} recorded this month). Raise it in "
                f"Admin > Settings, or wait for the next calendar month."
            )
        self._warn_if_budget_is_running_out(budget, spent)

    def _warn_if_budget_is_running_out(self, budget: float, spent: float) -> None:
        """Say something before the ceiling, not at it.

        Nothing watched the spend: the first sign of trouble was a batch
        refused halfway through, and the figure lived only in the provider's
        dashboard. Warning once per calendar month, on the call that crosses
        the threshold - `spent` is already in hand, so this costs no query
        unless it actually fires.
        """
        fraction = self.store.get_float("ai.budget_warn_fraction", 0.0)
        if fraction <= 0 or spent < budget * fraction:
            return

        month = datetime.now(timezone.utc).strftime("%Y-%m")
        if self.store.get_str("ai.budget_warned_month", "") == month:
            return

        # Written before the message goes out. If sending fails, the admin gets
        # one log entry rather than a warning on every subsequent call.
        self.store.set("ai.budget_warned_month", month)
        self.db.commit()

        message = (
            f"AI spend for {month} is USD {spent:.2f} of the USD {budget:.2f} "
            f"monthly budget ({spent / budget:.0%}). Calls are refused outright "
            f"once the budget is reached."
        )
        log_error(
            self.db,
            source="ai_budget",
            message=message,
            level="warning",
            context={"month": month, "spent_usd": round(spent, 4), "budget_usd": budget},
        )
        logger.warning(message)
        self._email_admins("RACE Exam Simulator - AI budget warning", message)

    def _email_admins(self, subject: str, body: str) -> None:
        """Best effort. Email being off or misconfigured must never be the
        reason an AI call fails - the log entry above is the real record."""
        from app.constants import ROLE_ADMIN
        from app.models import User
        from app.services.email import send_email

        try:
            admins = (
                self.db.execute(
                    select(User.email).where(User.role == ROLE_ADMIN, User.is_active.is_(True))
                )
                .scalars()
                .all()
            )
            for address in admins:
                send_email(self.db, to=address, subject=subject, text_body=body)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not email the budget warning: %s", exc)

    # --- Telemetry --------------------------------------------------------
    def _log_call(
        self,
        task: str,
        response: AIResponse,
        status: str,
        error: str | None,
        job_id: int | None,
    ) -> None:
        try:
            self.db.add(
                AiCall(
                    created_at=datetime.now(timezone.utc),
                    task=task,
                    provider=response.provider or "unknown",
                    model=response.model,
                    prompt_tokens=response.prompt_tokens,
                    completion_tokens=response.completion_tokens,
                    cost_usd=response.cost_usd,
                    latency_ms=response.latency_ms,
                    status=status,
                    error=error[:4000] if error else None,
                    job_id=job_id,
                )
            )
            self.db.commit()
        except Exception:  # noqa: BLE001 - telemetry must never break the caller
            logger.exception("Failed to record AI call")
            self.db.rollback()


def _is_retryable(exc: Exception) -> bool:
    text = str(exc).lower()
    if "timeout" in text or "network error" in text:
        return True
    return any(
        code in text
        for code in ("http 429", "http 500", "http 502", "http 503", "http 504", "http 529")
    )


def get_ai_client(db: Session) -> AIClient:
    return AIClient(db)
