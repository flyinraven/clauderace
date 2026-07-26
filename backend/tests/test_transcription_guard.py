"""The hallucination guard on spoken answers.

A transcript longer than the recording could physically contain was invented,
not heard - and an invented answer attributed to the candidate is worse than no
transcript, because it gets marked as though they said it.
"""

from __future__ import annotations

from app.services.osce.transcribe import (
    MIN_FLAGGED_WORDS,
    TRANSCRIBE_PROMPT,
    looks_hallucinated,
)


def words(n: int) -> str:
    return " ".join(["word"] * n)


def test_normal_speech_is_not_flagged():
    # 30 words in 15 seconds is 2 words/second - ordinary speech.
    assert looks_hallucinated(words(30), 15_000) is None


def test_brisk_speech_is_not_flagged():
    # 45 words in 15 seconds is 3/second: fast, but people do talk like that.
    assert looks_hallucinated(words(45), 15_000) is None


def test_paragraphs_from_a_short_recording_are_flagged():
    # The reported failure: a long answer invented over a brief recording.
    reason = looks_hallucinated(words(300), 15_000)
    assert reason is not None
    assert "300 words" in reason
    assert "invented" in reason


def test_short_transcripts_are_never_flagged():
    # Below the floor the ratio is too noisy to mean anything.
    assert looks_hallucinated(words(MIN_FLAGGED_WORDS - 1), 1_000) is None


def test_missing_duration_falls_back_to_file_size():
    # The guard must not be silently disabled when the browser omits duration.
    assert looks_hallucinated(words(400), None, audio_bytes=40_000) is not None
    assert looks_hallucinated(words(20), None, audio_bytes=400_000) is None


def test_no_duration_and_no_size_cannot_be_judged():
    assert looks_hallucinated(words(500), None, None) is None


def test_prompt_does_not_prime_the_model_with_content():
    """The original prompt listed eponyms to 'expect', which is precisely what
    let the model fabricate a plausible ophthalmology answer from silence."""
    lowered = TRANSCRIBE_PROMPT.lower()
    for primer in ("krukenberg", "vogt", "hutchinson", "expect clinical"):
        assert primer not in lowered
    assert "only words you can actually hear" in lowered
    assert "empty string" in lowered
