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
Transcribe the speech in this audio, word for word.

You are a transcriber, not an assistant. You have no knowledge of what the \
speaker was asked or what a good answer would contain, and you must not use \
any such knowledge if you think you do.

Absolute rules:
- Write ONLY words you can actually hear. Never infer, complete, expand, \
correct, summarise or improve.
- If the audio is silent, too quiet, or contains no intelligible speech, \
return an empty string. Returning nothing is always better than guessing.
- If you can hear only a few words, transcribe only those few words. A short \
recording must produce a short transcript.
- Never continue a sentence the speaker did not finish.
- Mark unclear stretches [inaudible] rather than filling them in.

The speaker is a doctor, so spell medical terms you clearly hear using \
standard clinical spelling, and write spoken visual acuities in the usual \
notation (\"six over twelve\" becomes 6/12). This applies only to words you \
actually hear - it is not a licence to introduce medical content.

Return ONLY the transcript text, with no preamble, commentary or explanation."""

# Conversational speech runs at roughly 2.5 words per second and even a fast
# talker rarely sustains 3.5. Beyond that the transcript contains more than the
# recording could hold, so it was invented rather than heard.
MAX_WORDS_PER_SECOND = 3.5
MIN_FLAGGED_WORDS = 20
MIN_AUDIO_BYTES = 2000
# Used only when the browser did not report a duration: compressed speech is
# comfortably above 8 kB/s, so this under-estimates length and therefore only
# ever makes the check more forgiving.
ASSUMED_BYTES_PER_SECOND = 8000


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


def looks_hallucinated(
    text: str, duration_ms: int | None, audio_bytes: int | None = None
) -> str | None:
    """Return a reason if a transcript is too long for the audio it came from.

    Audio models fill silence with plausible invention rather than returning
    nothing, and a fabricated answer attributed to the candidate is worse than
    no transcript at all - it would be marked as though they had said it.

    Falls back to estimating length from the file size when the browser did not
    report a duration, so the check cannot be silently disabled by a missing
    field.
    """
    words = len(text.split())
    if words < MIN_FLAGGED_WORDS:
        return None

    if duration_ms and duration_ms > 0:
        seconds = duration_ms / 1000
        source = "of audio"
    elif audio_bytes:
        seconds = audio_bytes / ASSUMED_BYTES_PER_SECOND
        source = "of audio (estimated from file size)"
    else:
        return None

    if seconds <= 0 or words <= seconds * MAX_WORDS_PER_SECOND:
        return None

    return (
        f"{words} words were transcribed from {seconds:.0f} seconds {source} - "
        f"about {words / seconds:.1f} words a second. That is faster than anyone "
        f"speaks, so some of this was probably invented rather than heard. "
        f"Delete anything you did not say before submitting."
    )


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

    # A clip this small holds no speech. Sending it invites the model to invent
    # an answer rather than admit it heard nothing.
    if len(clip.data) < MIN_AUDIO_BYTES:
        response.transcript = ""
        response.transcription_status = "complete"
        response.transcription_error = (
            "The recording was empty or near-silent, so nothing was transcribed."
        )
        db.commit()
        return ""

    try:
        text = transcribe_audio(db, clip.data, clip.content_type, store)
    except AIError as exc:
        response.transcription_status = "failed"
        response.transcription_error = str(exc)[:2000]
        db.commit()
        raise

    suspicious = looks_hallucinated(
        text, response.duration_ms or clip.duration_ms, clip.size_bytes
    )

    response.transcript = text
    response.transcription_status = "complete"
    response.transcription_error = suspicious

    if not store.get_bool("osce.retain_audio", False):
        clip.data = None
        clip.is_discarded = True

    db.commit()
    return text
