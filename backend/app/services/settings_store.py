"""Admin-editable runtime settings, backed by the `settings` table.

Anything an administrator can change without a redeploy lives here: AI provider
and models, image search credentials, SMTP. Secrets are Fernet-encrypted at
rest and only ever leave the API masked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Setting
from app.security import decrypt_secret, encrypt_secret, mask_secret


@dataclass(frozen=True)
class SettingSpec:
    key: str
    default: Any
    label: str
    group: str
    is_secret: bool = False
    help_text: str = ""
    choices: list[str] = field(default_factory=list)


# Default models are chosen to be cheap-but-capable on OpenRouter. An admin can
# override any of them, including pointing at a different provider entirely.
SETTING_SPECS: list[SettingSpec] = [
    # --- AI provider ------------------------------------------------------
    SettingSpec(
        "ai.provider", "openrouter", "Primary provider", "ai",
        choices=["openrouter", "openai", "anthropic", "google", "custom"],
        help_text="OpenRouter, OpenAI, Google AI Studio and any OpenAI-compatible "
                  "endpoint use the same request format. Anthropic uses its native API.",
    ),
    SettingSpec(
        "ai.base_url", "", "Primary base URL override", "ai",
        help_text="Leave blank to use the provider default. Required for 'custom'.",
    ),
    SettingSpec("ai.api_key", "", "Primary API key", "ai", is_secret=True),

    # --- Second provider slot --------------------------------------------
    SettingSpec(
        "ai.provider2", "google", "Secondary provider", "ai2",
        choices=["none", "openrouter", "openai", "anthropic", "google", "custom"],
        help_text="A second provider with its own key. Each task below chooses "
                  "which provider serves it, so you can run Gemini directly on a "
                  "Google AI Studio key while Claude comes through OpenRouter.",
    ),
    SettingSpec(
        "ai.base_url2", "", "Secondary base URL override", "ai2",
        help_text="Blank uses the provider default. Google AI Studio is "
                  "https://generativelanguage.googleapis.com/v1beta/openai",
    ),
    SettingSpec("ai.api_key2", "", "Secondary API key", "ai2", is_secret=True),

    # --- Per-task model and routing ---------------------------------------
    SettingSpec(
        "ai.model.structuring", "gemini-2.5-flash",
        "Model - document structuring", "ai",
        help_text="Parses uploaded past papers into structured questions. "
                  "Transcription work - a fast, cheap model is fine.",
    ),
    SettingSpec(
        "ai.model.structuring.slot", "primary", "  ↳ provider", "ai",
        choices=["primary", "secondary"],
        help_text="Switch to 'secondary' once the secondary provider below has a "
                  "key. Model ids differ per provider: Google AI Studio wants "
                  "'gemini-2.5-flash', OpenRouter wants 'google/gemini-2.5-flash'.",
    ),
    SettingSpec(
        "ai.model.model_answer", "anthropic/claude-sonnet-5",
        "Model - model answers", "ai",
        help_text="Writes examiner-grade marking keys. Use your strongest model; "
                  "it must accept images to read the clinical figures.",
    ),
    SettingSpec(
        "ai.model.model_answer.slot", "primary", "  ↳ provider", "ai",
        choices=["primary", "secondary"],
    ),
    SettingSpec(
        "ai.model.generation", "anthropic/claude-sonnet-5",
        "Model - question generation", "ai",
        help_text="Inventing new clinical questions. Genuinely needs a strong "
                  "model - this is the one place cheap output shows.",
    ),
    SettingSpec(
        "ai.model.generation.slot", "primary", "  ↳ provider", "ai",
        choices=["primary", "secondary"],
    ),
    SettingSpec(
        "ai.model.utility", "google/gemini-2.5-flash",
        "Model - utility tasks", "ai",
        help_text="Mechanical work that reorganises content you already have: "
                  "building OSCE examiner prompts, splitting findings, writing "
                  "image search phrases. Roughly 10x cheaper and no quality cost.",
    ),
    SettingSpec(
        "ai.model.utility.slot", "primary", "  ↳ provider", "ai",
        choices=["primary", "secondary"],
    ),
    SettingSpec(
        "ai.model.grading", "anthropic/claude-sonnet-5",
        "Model - grading", "ai",
        help_text="Run twice per answer to simulate two examiners.",
    ),
    SettingSpec(
        "ai.model.grading.slot", "primary", "  ↳ provider", "ai",
        choices=["primary", "secondary"],
    ),
    SettingSpec(
        "ai.model.vision", "google/gemini-2.5-flash",
        "Model - image analysis", "ai",
        help_text="Must accept image input. Used to check a sourced image really "
                  "shows a station's signs - a screening judgement, not clinical "
                  "reasoning, so a cheap vision model is the right tool.",
    ),
    SettingSpec(
        "ai.model.vision.slot", "primary", "  ↳ provider", "ai",
        choices=["primary", "secondary"],
    ),
    SettingSpec(
        "ai.monthly_budget_usd", 25.0, "Monthly budget (USD)", "ai",
        help_text="Hard ceiling on AI spend per calendar month. Calls are refused "
                  "once it is reached. Only providers that report per-call cost "
                  "(OpenRouter does) are counted, so treat it as a floor, not an "
                  "exact figure. Set 0 to disable.",
    ),
    SettingSpec(
        "ai.google_reasoning_effort", "none", "Google reasoning effort", "ai2",
        choices=["none", "low", "medium", "high", "default"],
        help_text="Gemini 2.5 models think before answering, and thinking tokens "
                  "count against Max output tokens - unconstrained, a short "
                  "request can return nothing at all. 'none' suits transcription "
                  "and marking against an explicit key. Ignored by other providers.",
    ),
    SettingSpec("ai.temperature", 0.3, "Temperature", "ai"),
    SettingSpec("ai.max_tokens", 8000, "Max output tokens", "ai"),
    SettingSpec("ai.timeout_seconds", 180, "Request timeout (s)", "ai"),
    SettingSpec("ai.max_retries", 3, "Max retries", "ai"),

    # --- Image search -----------------------------------------------------
    SettingSpec(
        "imagesearch.provider", "brave", "Image search provider", "images",
        choices=["brave", "serpapi", "openverse", "none"],
        help_text="Brave searches the open web (paid, ~$5 per 1000 queries with "
                  "$5 monthly credit). SerpApi proxies Google Images and needs "
                  "its own key (paid, no free tier beyond a small trial). "
                  "Openverse is free and needs no key but returns only openly "
                  "licensed images, so clinical photographs are scarce. 'none' "
                  "disables sourcing entirely. Google Custom Search and the "
                  "Bing Search API are deliberately absent: Bing was retired in "
                  "August 2025 and Google Custom Search is closed to new "
                  "customers and retires 1 Jan 2027.",
    ),
    SettingSpec(
        "imagesearch.api_key", "", "Image search API key", "images", is_secret=True,
        help_text="Not required for Openverse.",
    ),
    SettingSpec(
        "imagesearch.monthly_query_limit", 500, "Monthly query limit", "images",
        help_text="Hard ceiling on searches per calendar month. Brave bills "
                  "overages with no spending cap of its own, so this is your "
                  "protection against a runaway batch. Set 0 to disable.",
    ),
    SettingSpec("imagesearch.auto_attach", True, "Auto-attach best match", "images"),
    SettingSpec(
        "imagesearch.auto_approve", True, "Show images without waiting for approval",
        "images",
        help_text="On: a verified image appears at its station straight away, and "
                  "you reject the ones that are wrong. Off: nothing is shown until "
                  "you approve it, which means stations start with no image at all.",
    ),
    SettingSpec("imagesearch.results_per_query", 6, "Candidates per query", "images"),

    # --- Email ------------------------------------------------------------
    SettingSpec(
        "smtp.host", "", "SMTP host", "email",
        help_text="e.g. mail.txglobal.com.au for a SiteGround mailbox.",
    ),
    SettingSpec(
        "smtp.port", 465, "SMTP port", "email",
        help_text="465 with SSL on, or 587 with SSL off (the connection still "
                  "upgrades to STARTTLS).",
    ),
    SettingSpec("smtp.use_ssl", True, "Use SSL", "email"),
    SettingSpec(
        "smtp.username", "", "SMTP username", "email",
        help_text="Usually the full mailbox address.",
    ),
    SettingSpec("smtp.password", "", "SMTP password", "email", is_secret=True),
    SettingSpec(
        "smtp.from_address", "", "From address", "email",
        help_text="Blank uses the username. Must be a mailbox the server is "
                  "willing to send as, or the message is refused.",
    ),
    SettingSpec("smtp.from_name", "RACE Exam Simulator", "From name", "email"),
    SettingSpec("smtp.timeout_seconds", 20, "Connection timeout (s)", "email"),
    SettingSpec(
        "smtp.enabled", False, "Send emails", "email",
        help_text="Off: invite codes are created but never emailed, and you copy "
                  "them by hand. On: an invite with an email address is sent as "
                  "soon as it is created.",
    ),
    SettingSpec(
        "app.public_url", "", "Public site URL", "email",
        help_text="Where candidates reach the site, e.g. "
                  "https://exam.txglobal.com.au. Used to build the sign-up link "
                  "in invite emails; blank sends the bare code instead.",
    ),

    # --- OSCE -------------------------------------------------------------
    SettingSpec(
        "osce.transcription_route", "auto", "Speech-to-text route", "osce",
        choices=["auto", "openrouter", "google"],
        help_text="'auto' sends WAV/MP3 through OpenRouter and everything else "
                  "(including the audio/mp4 iOS Safari records) through Google's "
                  "native API. Google AI Studio's FREE tier allows only 20 "
                  "requests per day, so the Google route needs billing enabled.",
    ),
    SettingSpec(
        "ai.model.transcription_openrouter", "google/gemini-2.5-flash",
        "Model - speech to text (OpenRouter)", "osce",
        help_text="Must accept audio input. Uses the primary provider's key.",
    ),
    SettingSpec(
        "ai.model.transcription", "gemini-2.5-flash",
        "Model - speech to text (Google native)", "osce",
        help_text="Used for containers OpenRouter cannot carry, e.g. the "
                  "audio/mp4 recorded by iOS Safari.",
    ),
    SettingSpec(
        "osce.retain_audio", False, "Keep recordings after transcription", "osce",
        help_text="Off by default. A nine-station circuit records about 45 clips "
                  "a day, which would fill the hosting quota for no benefit once "
                  "the transcript exists.",
    ),
    SettingSpec(
        "osce.stations_per_circuit", 9, "Stations per daily circuit", "osce",
        help_text="One per subspecialty by default.",
    ),
    SettingSpec(
        "osce.auto_grade_on_submit", True, "Mark automatically on submit", "osce",
    ),

    # --- Exam behaviour ---------------------------------------------------
    SettingSpec(
        "grading.examiner_passes", 1, "Examiner passes per answer", "exam",
        help_text="The real exam is marked by two examiners, and running two "
                  "passes reproduces that - including flagging where they "
                  "disagree. It also doubles the cost of every paper. One pass "
                  "is the sensible default for solo revision; set 2 when you "
                  "want the disagreement signal.",
    ),
    SettingSpec(
        "exam.auto_grade_on_submit", True, "Grade automatically on submit", "exam",
    ),
    SettingSpec(
        "exam.allow_untimed_practice", True, "Allow untimed practice mode", "exam",
    ),
]

SPECS_BY_KEY: dict[str, SettingSpec] = {s.key: s for s in SETTING_SPECS}


class SettingsStore:
    """Read/write access to runtime settings for one request or job."""

    def __init__(self, db: Session):
        self.db = db
        self._cache: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._cache is None:
            rows = self.db.execute(select(Setting)).scalars().all()
            self._cache = {r.key: (r.value, r.is_encrypted) for r in rows}
        return self._cache

    def get(self, key: str, default: Any = None) -> Any:
        """Return the effective value, decrypting secrets."""
        spec = SPECS_BY_KEY.get(key)
        rows = self._load()
        if key not in rows:
            return spec.default if spec else default
        value, is_encrypted = rows[key]
        if is_encrypted and isinstance(value, str) and value:
            return decrypt_secret(value) or ""
        if value is None or value == "":
            # An empty stored value falls back to the default so clearing a
            # field in the UI restores sensible behaviour.
            return spec.default if spec else default
        return value

    def get_str(self, key: str, default: str = "") -> str:
        value = self.get(key, default)
        return str(value) if value is not None else default

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.get(key, default))
        except (TypeError, ValueError):
            return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.get(key, default))
        except (TypeError, ValueError):
            return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        value = self.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def set(self, key: str, value: Any, updated_by_id: int | None = None) -> None:
        spec = SPECS_BY_KEY.get(key)
        is_secret = bool(spec and spec.is_secret)
        stored: Any = value
        if is_secret:
            stored = encrypt_secret(str(value)) if value else ""
        row = self.db.get(Setting, key)
        if row is None:
            row = Setting(key=key, value=stored, is_encrypted=is_secret)
            self.db.add(row)
        else:
            row.value = stored
            row.is_encrypted = is_secret
        row.updated_by_id = updated_by_id
        self._cache = None

    def describe_all(self) -> list[dict[str, Any]]:
        """Admin-facing view: secrets masked, never returned in full."""
        out: list[dict[str, Any]] = []
        for spec in SETTING_SPECS:
            raw = self.get(spec.key)
            out.append(
                {
                    "key": spec.key,
                    "label": spec.label,
                    "group": spec.group,
                    "help_text": spec.help_text,
                    "choices": spec.choices,
                    "is_secret": spec.is_secret,
                    "value": mask_secret(str(raw)) if spec.is_secret else raw,
                    "is_set": bool(raw),
                }
            )
        return out
