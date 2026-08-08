"""The examiner may state the diagnosis, but only after asking for a differential.

Stating it mid-station is what the real handouts do, and for a good reason: it
stops the later marks depending on whether the candidate got the diagnosis. Lisa
Cooke is told she carries the 11778 mutation and then asked what that means.

But the reveal always lands *after* the candidate has been made to reason.
"What are your differential diagnoses so far?" comes first, and the marking key
groups the answers - hereditary, compressive, inflammatory, infective. Fifty-six
stations in the bank instead asked for "your leading diagnosis" and then
announced the answer, so the candidate names one thing and is never asked to
think across possibilities at all.
"""

from __future__ import annotations

from app.services.osce.prompts import ask_for_differentials, needs_differential_first


def _q(text: str) -> dict:
    return {"text": text}


def test_a_reveal_with_no_differential_asked_is_found():
    prompts = [
        _q("Please examine the ocular movements of this patient."),
        _q("Can you summarise your findings and give me your diagnosis?"),
        _q("The diagnosis is Bilateral Brown's Syndrome. How would you manage her?"),
    ]
    assert needs_differential_first(prompts) == 2


def test_a_reveal_after_a_differential_is_left_alone():
    """A hundred stations already do it correctly and must not be touched."""
    prompts = [
        _q("Please examine the anterior segment of both eyes."),
        _q("Can you summarise your findings and give me 3 differential diagnoses?"),
        _q("The diagnosis is keratoconus in the left eye. How would you manage him?"),
    ]
    assert needs_differential_first(prompts) is None


def test_a_station_that_never_reveals_is_left_alone():
    prompts = [
        _q("Please examine the fundus of both eyes."),
        _q("What is the presumed diagnosis?"),
        _q("How would you confirm it?"),
    ]
    assert needs_differential_first(prompts) is None


def test_the_eight_wordings_the_bank_uses_all_convert():
    for text in (
        "Can you summarise your findings and give me your leading diagnosis?",
        "Can you summarise your findings and give me your diagnosis?",
        "Can you summarise your findings and give me a diagnosis?",
        "Can you summarise your findings and give me the diagnosis?",
        "Please summarise your findings and give me your leading diagnosis.",
        "Can you summarise your findings and give me your unifying diagnosis?",
    ):
        rewritten = ask_for_differentials(text)
        assert "differential diagnoses" in rewritten, text
        assert rewritten.endswith(("?", ".")), text


def test_the_favoured_diagnosis_is_still_asked_for():
    """The rubric for these questions awards a mark for naming the diagnosis.
    Dropping it would leave a marking key asking for something the question no
    longer requests - the fault this whole exercise exists to undo."""
    rewritten = ask_for_differentials(
        "Can you summarise your findings and give me your leading diagnosis?"
    )
    assert "which you favour" in rewritten


def test_a_question_that_does_not_name_a_diagnosis_gets_the_ask_added():
    rewritten = ask_for_differentials("Can you summarise your findings for me?")
    assert rewritten == (
        "Can you summarise your findings for me? And what are your differential diagnoses?"
    )


def test_a_question_already_asking_for_differentials_is_untouched():
    text = "Can you summarise your findings and give me 3 differential diagnoses?"
    assert ask_for_differentials(text) == text
