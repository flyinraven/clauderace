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
    not_speech,
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


def test_the_prompt_coming_back_is_not_an_answer():
    """Seen live on 13 Aug 2026: four questions across one circuit were marked
    against our own transcriber instructions, echoed back verbatim."""
    reason = not_speech(TRANSCRIBE_PROMPT)
    assert reason is not None
    assert "instructions" in reason

    # It comes back without the opening line as often as with it.
    without_first_line = TRANSCRIBE_PROMPT.split("\n\n", 1)[1]
    assert not_speech(without_first_line) is not None


def test_a_looping_phrase_is_discarded():
    looped = "I'm going to be looking forward for me. " * 40
    reason = not_speech(looped)
    assert reason is not None
    assert "looped" in reason


def test_a_repeated_phrase_that_does_not_dominate_is_kept():
    real = (
        "There is band keratopathy in the interpalpebral fissure. "
        "The lens shows a posterior subcapsular cataract. "
        "I would check the pressure. I would check the pressure. "
        "The disc is cupped and the rim is thin superiorly. "
        "There are posterior synechiae at six o'clock. "
        "I would examine the fellow eye as well."
    )
    assert not_speech(real) is None


def test_ordinary_answers_survive():
    assert not_speech("Radial keratotomy incisions in both corneas.") is None
    assert not_speech("") is None


def test_prompt_does_not_prime_the_model_with_content():
    """The original prompt listed eponyms to 'expect', which is precisely what
    let the model fabricate a plausible ophthalmology answer from silence."""
    lowered = TRANSCRIBE_PROMPT.lower()
    for primer in ("krukenberg", "vogt", "hutchinson", "expect clinical"):
        assert primer not in lowered
    assert "only words you can actually hear" in lowered
    assert "empty string" in lowered
