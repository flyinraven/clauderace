"""Speech-to-text for spoken OSCE answers.

Uses Gemini's native `generateContent` endpoint rather than the OpenAI
compatibility layer, because inline audio is only reliably supported on the
native API. The audio itself never leaves the candidate's own Google key.

Browsers disagree on container format - iOS Safari's MediaRecorder emits
audio/mp4 (AAC) and only that, while Chromium emits audio/webm (Opus) - so the
declared MIME type is mapped to something Gemini accepts rather than assumed.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.models import AudioClip, OsceResponse
from app.services.ai.client import AIError
from app.services.settings_store import SettingsStore

logger = logging.getLogger(__name__)

GEMINI_NATIVE_BASE = "https://generativelanguage.googleapis.com/v1beta"

# Gemini's accepted audio MIME types, keyed by what browsers actually send.
MIME_MAP = {
    "audio/mp4": "audio/mp4",
    "audio/m4a": "audio/mp4",
    "audio/x-m4a": "audio/mp4",
    "audio/aac": "audio/aac",
    "audio/mpeg": "audio/mp3",
    "audio/mp3": "audio/mp3",
    "audio/webm": "audio/webm",
    "audio/ogg": "audio/ogg",
    "audio/wav": "audio/wav",
    "audio/x-wav": "audio/wav",
    "audio/flac": "audio/flac",
}

# Inline audio must fit inside the request; Gemini's inline ceiling is 20 MB.
MAX_INLINE_BYTES = 18 * 1024 * 1024

TRANSCRIBE_PROMPT = """\
Transcribe this audio verbatim. It is an ophthalmology trainee answering an \
examiner's question aloud in a mock RANZCO OSCE, so expect clinical \
terminology: drug names, eponyms (Krukenberg spindle, Vogt striae, \
Hutchinson's sign), abbreviations spoken as letters (IOP, OCT, FFA, RAPD, \
PCIOL), and Snellen acuities said as "six over twelve" which you should write \
as 6/12.

Rules:
- Write exactly what was said. Do not summarise, correct, complete or improve \
the answer, and do not add anything that was not spoken.
- Use standard clinical spelling for terms you recognise.
- If a stretch is genuinely inaudible, write [inaudible].
- If there is no speech at all, return an empty string.

Return ONLY the transcript text, with no preamble or commentary."""


def _retry_after_seconds(response: httpx.Response) -> float:
    """Seconds Google asked us to wait, from the header or the RetryInfo body."""
    header = response.headers.get("retry-after")
    try:
        if header:
            return float(header)
    except (TypeError, ValueError):
        pass
    import re

    match = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', response.text)
    return float(match.group(1)) if match else 45.0


def normalise_mime(content_type: str | None) -> str:
    base = (content_type or "").split(";")[0].strip().lower()
    return MIME_MAP.get(base, "audio/mp4")


# OpenAI's input_audio content type only names these two formats, so anything
# else has to go via Google's native API which accepts containers directly.
OPENROUTER_AUDIO_FORMATS = {
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
}


def transcribe_audio(
    db: Session, data: bytes, content_type: str, store: SettingsStore | None = None
) -> str:
    """Transcribe one clip, choosing whichever provider can carry the format.

    Google AI Studio's free tier allows only 20 requests per day, which a
    single OSCE circuit exhausts twice over, so OpenRouter is preferred when it
    can accept the container. iOS Safari records audio/mp4, which the OpenAI
    input_audio schema does not name, so those clips go to Google's native
    endpoint - that path needs billing enabled on the Google project.
    """
    store = store or SettingsStore(db)
    route = store.get_str("osce.transcription_route", "auto").lower()

    openrouter_format = OPENROUTER_AUDIO_FORMATS.get(
        (content_type or "").split(";")[0].strip().lower()
    )
    use_openrouter = route == "openrouter" or (route == "auto" and openrouter_format)

    if use_openrouter and openrouter_format:
        return _transcribe_via_openrouter(db, data, openrouter_format, store)
    return _transcribe_via_google(db, data, content_type, store)


def _transcribe_via_openrouter(
    db: Session, data: bytes, audio_format: str, store: SettingsStore
) -> str:
    from app.services.ai import AIClient

    client = AIClient(db)
    model = store.get_str("ai.model.transcription_openrouter", "google/gemini-2.5-flash")
    content = [
        {"type": "text", "text": TRANSCRIBE_PROMPT},
        {
            "type": "input_audio",
            "input_audio": {"data": base64.b64encode(data).decode("ascii"), "format": audio_format},
        },
    ]
    provider = client.providers["primary"]
    if not provider.is_configured:
        raise AIError("The primary provider has no API key set, so audio cannot be sent to it.")

    payload = client._post(  # noqa: SLF001 - deliberate reuse of the shared HTTP path
        f"{provider.base_url}/chat/completions",
        {
            "Authorization": f"Bearer {provider.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://exam.txglobal.com.au",
            "X-Title": "RANZCO RACE Exam Simulator",
        },
        {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.0,
            "max_tokens": 4096,
        },
    )
    try:
        return (payload["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise AIError(f"Unexpected transcription response: {str(payload)[:300]}") from exc


def _transcribe_via_google(
    db: Session, data: bytes, content_type: str, store: SettingsStore
) -> str:
    api_key = store.get_str("ai.api_key2", "")
    provider = store.get_str("ai.provider2", "").lower()
    if provider != "google" or not api_key:
        if store.get_str("ai.provider", "").lower() == "google":
            api_key = store.get_str("ai.api_key", "")
        if not api_key:
            raise AIError(
                "This recording's format can only be transcribed by Google's native "
                "API, but no Google AI Studio key is configured."
            )

    if not data:
        return ""
    if len(data) > MAX_INLINE_BYTES:
        raise AIError(
            f"Recording is {len(data) // 1024 // 1024} MB; the inline limit is "
            f"{MAX_INLINE_BYTES // 1024 // 1024} MB. Answers should be shorter than this."
        )

    model = store.get_str("ai.model.transcription", "gemini-2.5-flash")
    url = f"{GEMINI_NATIVE_BASE}/models/{model}:generateContent"

    body: dict[str, Any] = {
        "contents": [
            {
                "parts": [
                    {"text": TRANSCRIBE_PROMPT},
                    {
                        "inline_data": {
                            "mime_type": normalise_mime(content_type),
                            "data": base64.b64encode(data).decode("ascii"),
                        }
                    },
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": 4096,
            # Transcription is not a reasoning task; thinking would eat the
            # output budget and can return an empty transcript.
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }

    timeout = store.get_int("ai.timeout_seconds", 180)
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                url,
                headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                json=body,
            )
    except httpx.TimeoutException as exc:
        raise AIError(f"Transcription timed out after {timeout}s") from exc
    except httpx.HTTPError as exc:
        raise AIError(f"Transcription network error: {exc}") from exc

    if response.status_code == 429:
        # Keep Google's own text: it names the quota that was hit (per-minute
        # versus per-day), which decides whether waiting helps at all.
        raise AIError(
            f"Transcription rate limit (HTTP 429): {response.text[:600]}",
            retry_after=_retry_after_seconds(response),
        )
    if response.status_code >= 400:
        raise AIError(f"Transcription HTTP {response.status_code}: {response.text[:400]}")

    try:
        payload = response.json()
        candidates = payload.get("candidates") or []
        if not candidates:
            # A safety block returns no candidate at all.
            reason = (payload.get("promptFeedback") or {}).get("blockReason")
            raise AIError(f"Transcription returned no result (blockReason={reason!r}).")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts)
    except AIError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise AIError(f"Unexpected transcription response: {response.text[:300]}") from exc

    return text.strip()


def transcribe_response(db: Session, response: OsceResponse) -> str:
    """Transcribe a stored response, then release its audio.

    The recording is dropped once the transcript exists unless retention is
    enabled: a nine-station circuit generates roughly 45 clips a day, which
    would consume the hosting quota for no ongoing benefit.
    """
    store = SettingsStore(db)
    clip = db.get(AudioClip, response.audio_clip_id) if response.audio_clip_id else None
    if clip is None or not clip.data:
        response.transcription_status = "failed"
        response.transcription_error = "No audio was recorded for this answer."
        db.commit()
        return ""

    try:
        text = transcribe_audio(db, clip.data, clip.content_type, store)
    except AIError as exc:
        response.transcription_status = "failed"
        response.transcription_error = str(exc)[:2000]
        db.commit()
        raise

    response.transcript = text
    response.transcription_status = "complete"
    response.transcription_error = None

    if not store.get_bool("osce.retain_audio", False):
        clip.data = None
        clip.is_discarded = True

    db.commit()
    return text
