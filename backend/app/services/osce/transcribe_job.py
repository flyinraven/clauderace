"""Background transcription of a recorded OSCE answer.

Runs as a job so the upload request returns immediately: the candidate moves on
to the next examiner question while their previous answer is transcribed.
"""

from __future__ import annotations

import logging

from app.models import OsceResponse
from app.services.errors import log_error
from app.services.jobs.runner import JobContext, JobHandlerError, register_handler
from app.services.osce.transcribe import transcribe_response

logger = logging.getLogger(__name__)

JOB_TRANSCRIBE_RESPONSE = "transcribe_response"


@register_handler(JOB_TRANSCRIBE_RESPONSE)
def handle_transcribe_response(ctx: JobContext) -> bool:
    response_id = ctx.payload.get("response_id")
    if not response_id:
        raise JobHandlerError("Transcription job is missing response_id")

    response = ctx.db.get(OsceResponse, response_id)
    if response is None:
        raise JobHandlerError(f"Response {response_id} no longer exists")

    if response.transcription_status == "complete":
        ctx.advance(1, "Already transcribed")
        return True

    try:
        text = transcribe_response(ctx.db, response)
        ctx.set_result(characters=len(text), prompt=response.prompt_label)
        ctx.advance(1, f"Transcribed answer {response.prompt_label}")
    except Exception as exc:  # noqa: BLE001 - recorded on the response itself
        ctx.db.rollback()
        logger.exception("Transcription failed for response %s", response_id)
        log_error(
            ctx.db, source="transcription", message=str(exc),
            context={"response_id": response_id},
        )
        # Re-raise so the job runner retries: a 429 here is usually transient,
        # and the candidate can also retry from the review screen.
        raise

    return True
