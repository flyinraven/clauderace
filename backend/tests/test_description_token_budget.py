"""The spoken findings must not carry a token ceiling of their own.

This is the protocol's last resort: when no photograph can be found, the station
tells the candidate what the patient demonstrates in words, and every rubric
point has to be earnable from that text. A ceiling set here has been guessed too
low twice - 320, then 900 - and each time the reply was cut off mid-JSON, which
surfaces as "Could not describe findings" and looks like the model refusing
rather than the budget running out. Deferring to `ai.max_tokens` makes the
truncation error's own advice ("raise Max output tokens in Settings") true.
"""

from __future__ import annotations

from app.models import OsceStation
from app.services.osce.station_images.describe import describe_findings


class _RecordingClient:
    """Captures the kwargs of the description call and returns a valid reply."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def complete_json(self, task, system, user, **kwargs):
        self.calls.append({"task": task, **kwargs})
        return {"description": "The right eye shows a dense white cataract."}


def _station() -> OsceStation:
    return OsceStation(
        subspecialty="Cataract",
        findings_elicited="The right lens is opaque.",
        diagnosis="Traumatic cataract.",
    )


def test_no_token_ceiling_is_imposed_on_the_description() -> None:
    client = _RecordingClient()

    describe_findings(client, _station(), "Identify the traumatic cataract.")

    assert client.calls, "the description call was never made"
    assert client.calls[0].get("max_tokens") is None, (
        "describe_findings must not set max_tokens - the configured "
        "ai.max_tokens governs, so an administrator raising it actually helps"
    )


def test_the_description_is_billed_as_a_model_answer_not_a_utility_call() -> None:
    """Candidates are examined on this text, so it routes to the better model."""
    client = _RecordingClient()

    describe_findings(client, _station(), "Identify the traumatic cataract.")

    assert client.calls[0]["task"] == "model_answer"


def test_a_station_with_nothing_to_describe_makes_no_call_at_all() -> None:
    client = _RecordingClient()
    empty = OsceStation(subspecialty="Cataract")

    text, _ = describe_findings(client, empty, "")

    assert text is None
    assert client.calls == []
