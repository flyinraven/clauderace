"""The model gateway: routing, retries, JSON recovery, budget, and image size.

The image-size tests are the ones worth keeping. Every vision caller now goes
through `ImagePart`, and if that stops bounding what it sends, the next OSCE
image run quietly uploads tens of megabytes per station again.
"""

from __future__ import annotations

import io
import json

import pytest
from PIL import Image as PILImage

from app.services.ai.client import (
    AIClient,
    AIError,
    ImagePart,
    TextPart,
    parse_json_response,
)
from app.services.ai.images import MAX_EDGE, normalise_for_vision
from app.models import AiCall, Setting


def photo(width: int, height: int, fmt: str = "JPEG", mode: str = "RGB") -> bytes:
    """A noisy image, so it does not compress to nothing and stays realistic."""
    image = PILImage.new(mode, (width, height))
    pixels = image.load()
    for x in range(0, width, 3):
        for y in range(0, height, 3):
            value = ((x * 7) % 256, (y * 11) % 256, ((x + y) * 13) % 256)
            pixels[x, y] = value if mode == "RGB" else value + (255,)
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    return buffer.getvalue()


# --- Image bounding ------------------------------------------------------
def test_an_oversized_photograph_is_bounded_before_it_is_sent():
    original = photo(2600, 1950)
    part = ImagePart(data=original, media_type="image/jpeg")

    with PILImage.open(io.BytesIO(part.data)) as sent:
        assert max(sent.size) == MAX_EDGE
    assert len(part.data) < len(original) / 2
    assert part.media_type == "image/jpeg"


def test_bounding_happens_on_construction_so_no_caller_can_skip_it():
    """`data` is what will really be sent, which is what size checks assume."""
    part = ImagePart(data=photo(3000, 3000), media_type="image/jpeg")
    with PILImage.open(io.BytesIO(part.data)) as sent:
        assert sent.size == (MAX_EDGE, MAX_EDGE)


def test_an_image_already_within_the_cap_is_left_alone():
    original = photo(800, 600, fmt="PNG")
    part = ImagePart(data=original, media_type="image/png")
    # Either untouched, or re-encoded only because that came out smaller.
    assert len(part.data) <= len(original)
    with PILImage.open(io.BytesIO(part.data)) as sent:
        assert sent.size == (800, 600)


def test_something_that_is_not_an_image_passes_through_untouched():
    """A verification against the original beats no verification at all."""
    junk = b"not an image at all" * 5000
    data, media_type = normalise_for_vision(junk, "image/png")
    assert data == junk
    assert media_type == "image/png"


def test_a_transparent_png_is_flattened_and_still_capped():
    """A sparse PNG can be smaller than its downscaled JPEG and is still shrunk:
    providers charge for pixel area, not for bytes."""
    original = photo(2000, 2000, fmt="PNG", mode="RGBA")
    data, media_type = normalise_for_vision(original, "image/png")
    with PILImage.open(io.BytesIO(data)) as sent:
        assert max(sent.size) == MAX_EDGE
    assert media_type == "image/jpeg"


def test_a_tiny_image_is_not_worth_re_encoding():
    original = photo(120, 120, fmt="PNG")
    assert normalise_for_vision(original, "image/png") == (original, "image/png")


def test_the_bounded_bytes_are_what_reach_the_provider(db, monkeypatch):
    """End to end through the adapter: what lands in the request body is small."""
    _configure(db)
    sent: dict = {}

    def capture(self, url, headers, body):
        sent["body"] = body
        return {
            "model": "fake",
            "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
            "usage": {},
        }

    monkeypatch.setattr(AIClient, "_post", capture)
    original = photo(2600, 1950)
    AIClient(db).complete(
        task="vision",
        system="s",
        user=[TextPart("look"), ImagePart(data=original, media_type="image/jpeg")],
    )

    url = sent["body"]["messages"][-1]["content"][1]["image_url"]["url"]
    payload = url.split(",", 1)[1]
    # base64 inflates by a third, so compare against the encoded original.
    assert len(payload) < len(original)


# --- Configuration and routing -------------------------------------------
def _configure(db, **overrides) -> None:
    values = {
        "ai.provider": "openrouter",
        "ai.api_key": "key-1",
        "ai.model.structuring": "fake/flash",
        "ai.model.vision": "fake/flash",
        "ai.model.grading": "fake/flash",
    }
    values.update(overrides)
    for key, value in values.items():
        db.add(Setting(key=key, value=value, is_encrypted=False))
    db.commit()


def test_a_task_routed_to_an_unconfigured_slot_says_so_plainly(db):
    _configure(db, **{"ai.model.grading.slot": "secondary", "ai.provider2": "google"})
    with pytest.raises(AIError) as exc:
        AIClient(db).complete(task="grading", system="s", user="u")
    message = str(exc.value)
    assert "no API key set" in message
    assert "Admin > Settings" in message


def test_routing_never_silently_falls_back_to_the_other_provider(db):
    """Model ids are provider-specific; a silent fallback produces a 404 that
    sends you hunting for a prompt problem instead of a missing key."""
    _configure(db, **{"ai.model.vision.slot": "secondary", "ai.provider2": "none"})
    client = AIClient(db)
    assert client.is_configured, "the primary slot is configured"
    assert not client.is_configured_for("vision")


def test_an_unconfigured_task_uses_its_own_default_model(db):
    """Every task in `SETTING_SPECS` carries a default, so the structuring
    fallback in `model_for` only ever applies to a task with none."""
    _configure(db)
    assert AIClient(db).model_for("generation") == "anthropic/claude-sonnet-5"
    assert AIClient(db).model_for("a_task_that_does_not_exist") == "fake/flash"


# --- Retry and failure ---------------------------------------------------
def test_a_rate_limit_is_retried_and_the_providers_own_delay_is_honoured(db, monkeypatch):
    _configure(db, **{"ai.max_retries": 2, "ai.max_retry_delay_seconds": 0})
    attempts = {"n": 0}
    slept: list[float] = []

    def flaky(self, url, headers, body):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise AIError("HTTP 429: slow down", retry_after=30.0)
        return {
            "model": "fake",
            "choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        }

    monkeypatch.setattr(AIClient, "_post", flaky)
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))

    assert AIClient(db).complete_json(task="grading", system="s", user="u") == {"ok": True}
    assert attempts["n"] == 2
    assert slept, "a 429 must pause rather than retry immediately"


def test_a_bad_request_is_not_retried(db, monkeypatch):
    _configure(db, **{"ai.max_retries": 3})
    attempts = {"n": 0}

    def refuse(self, url, headers, body):
        attempts["n"] += 1
        raise AIError("HTTP 400: that model does not exist")

    monkeypatch.setattr(AIClient, "_post", refuse)
    with pytest.raises(AIError):
        AIClient(db).complete(task="grading", system="s", user="u")
    assert attempts["n"] == 1, "retrying a 400 just spends money to fail again"


def test_every_call_is_recorded_including_the_failures(db, monkeypatch):
    _configure(db, **{"ai.max_retries": 1})
    monkeypatch.setattr(
        AIClient,
        "_post",
        lambda self, url, headers, body: {
            "model": "fake",
            "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 3, "cost": 0.002},
        },
    )
    AIClient(db).complete(task="grading", system="s", user="u")

    monkeypatch.setattr(
        AIClient, "_post", lambda self, u, h, b: (_ for _ in ()).throw(AIError("HTTP 400: no"))
    )
    with pytest.raises(AIError):
        AIClient(db).complete(task="grading", system="s", user="u")

    calls = db.query(AiCall).all()
    assert [c.status for c in calls] == ["success", "error"]
    assert calls[0].prompt_tokens == 11
    assert calls[0].cost_usd == 0.002
    assert calls[1].error


def test_the_monthly_budget_stops_further_calls(db, monkeypatch):
    _configure(db, **{"ai.monthly_budget_usd": 0.001})
    monkeypatch.setattr(
        AIClient,
        "_post",
        lambda self, url, headers, body: {
            "model": "fake",
            "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.005},
        },
    )
    client = AIClient(db)
    client.complete(task="grading", system="s", user="u")

    with pytest.raises(AIError) as exc:
        AIClient(db).complete(task="grading", system="s", user="u")
    assert "budget" in str(exc.value)


def test_truncated_json_is_reported_as_a_token_limit_not_a_prompt_problem(db, monkeypatch):
    _configure(db, **{"ai.max_retries": 1})
    monkeypatch.setattr(
        AIClient,
        "_post",
        lambda self, url, headers, body: {
            "model": "fake",
            "choices": [
                {"message": {"content": '{"parts": [{"a"'}, "finish_reason": "length"}
            ],
            "usage": {},
        },
    )
    with pytest.raises(AIError) as exc:
        AIClient(db).complete_json(task="grading", system="s", user="u")
    assert "output limit" in str(exc.value)
    assert "Max output tokens" in str(exc.value)


def test_an_empty_reply_on_the_token_limit_names_the_thinking_budget(db, monkeypatch):
    _configure(db, **{"ai.max_retries": 1})
    monkeypatch.setattr(
        AIClient,
        "_post",
        lambda self, url, headers, body: {
            "model": "fake",
            "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
            "usage": {},
        },
    )
    with pytest.raises(AIError) as exc:
        AIClient(db).complete(task="grading", system="s", user="u")
    assert "thinking" in str(exc.value)


def test_malformed_json_is_repaired_with_one_extra_call(db, monkeypatch):
    _configure(db)
    replies = ["Certainly! Here you go: not json at all", '{"repaired": true}']
    calls: list[dict] = []

    def respond(self, url, headers, body):
        calls.append(body)
        return {
            "model": "fake",
            "choices": [
                {"message": {"content": replies[len(calls) - 1]}, "finish_reason": "stop"}
            ],
            "usage": {},
        }

    monkeypatch.setattr(AIClient, "_post", respond)
    assert AIClient(db).complete_json(task="grading", system="s", user="u") == {
        "repaired": True
    }
    assert len(calls) == 2
    assert "not valid JSON" in json.dumps(calls[1]["messages"][-1])


# --- JSON extraction -----------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('```\n{"a": 1}\n```', {"a": 1}),
        ('Here is the marking key:\n{"a": 1}', {"a": 1}),
        ('[{"a": 1}]', [{"a": 1}]),
        ('{"a": "a } brace in a string"}', {"a": "a } brace in a string"}),
        ('{"a": "an escaped \\" quote"}', {"a": 'an escaped " quote'}),
    ],
)
def test_json_is_recovered_from_the_shapes_models_actually_return(text, expected):
    assert parse_json_response(text) == expected


def test_unrecoverable_output_says_what_came_back(db):
    with pytest.raises(AIError) as exc:
        parse_json_response("I'm afraid I can't help with that request.")
    assert "can't help" in str(exc.value)
