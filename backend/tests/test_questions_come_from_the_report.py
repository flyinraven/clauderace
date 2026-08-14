"""The station must ask what the examiners' report says it was about.

Written after sitting a full circuit. Three faults, each seen on screen:

  - station 81 asked "You mentioned auscultation. What would you be listening
    for?" of a candidate who may never have mentioned it;
  - station 320 revealed "glaucoma secondary to Sturge Weber" and then asked
    the candidate to talk through the OCT for 4.5 marks;
  - eleven stations asked for "differential diagnoses for this patient's
    presentation" of patients with no stated presentation.

Every check here is calibrated against the live bank: each finds the real
cases and nothing else. The rejected/accepted pairs are the calibration.
"""

from __future__ import annotations

from app.services.osce.prompts import (
    _presupposes_an_answer,
    _reveal_before_the_reading,
    _written_from_the_arc_not_the_report,
)


def q(label, text, step=None, drawn_from="Aim: something", **kw):
    return {"label": label, "text": text, "step": step,
            "drawn_from": drawn_from, "rubric": [], **kw}


# --- a question that assumes an answer -------------------------------------

def test_you_mentioned_is_rejected():
    problems = _presupposes_an_answer(
        [q("B", "You mentioned auscultation. What would you be listening for?")]
    )
    assert len(problems) == 1
    assert "assumes an answer" in problems[0]


def test_asking_it_outright_is_accepted():
    assert _presupposes_an_answer(
        [q("B", "What would you listen for on auscultation, and what would it "
                "indicate?")]
    ) == []


# --- the reveal, and what may follow it ------------------------------------

def test_reading_a_test_after_the_reveal_is_rejected():
    problems = _reveal_before_the_reading([
        q("C", "The diagnosis is glaucoma secondary to Sturge Weber.", step=5),
        q("D", "This is her OCT and RNFL. Talk me through them.", step=3),
    ])
    assert len(problems) == 1
    assert "read a test" in problems[0]


def test_reaching_the_diagnosis_after_the_reveal_is_rejected():
    problems = _reveal_before_the_reading([
        q("E", "The diagnosis is diabetic macular oedema.", step=5),
        q("F", "Summarise your findings and give me your overall diagnosis.", step=4),
    ])
    assert len(problems) == 1
    assert "reach the diagnosis" in problems[0]


def test_a_hypothetical_after_the_reveal_is_accepted():
    """"What would you expect the B-scan to show" is asked after the diagnosis
    on purpose - it tests reasoning forward from it. Seven live questions have
    this shape and none of them is a fault."""
    assert _reveal_before_the_reading([
        q("D", "The diagnosis is a dense cataract.", step=5),
        q("E", "Given her dense cataract, what would you expect the B-scan "
               "ultrasound to show?", step=3),
        q("F", "What ancillary tests would you order if the view stayed poor?",
          step=3),
    ]) == []


def test_the_order_is_fine_when_the_reveal_comes_last():
    assert _reveal_before_the_reading([
        q("C", "This is her OCT. What does it show?", step=3),
        q("D", "Summarise and give three differentials for the swelling.", step=4),
        q("E", "The diagnosis is papilloedema. How would you manage her?", step=5),
    ]) == []


# --- written from the arc rather than the case -----------------------------

def test_a_differential_of_nothing_is_rejected():
    problems = _written_from_the_arc_not_the_report(
        [q("B", "Summarise your findings and give me three differential "
                "diagnoses for this patient's presentation.")]
    )
    assert any("stock sentence" in p for p in problems)


def test_a_differential_that_names_its_subject_is_accepted():
    assert _written_from_the_arc_not_the_report(
        [q("B", "Summarise your findings and give me three differential "
                "diagnoses for the cause of the corneal melt.")]
    ) == []


def test_the_reveal_wording_the_arc_asks_for_is_accepted():
    """140 stations say this and it is what a real examiner says. Rejecting it
    would reject the bank to fix nothing."""
    assert _written_from_the_arc_not_the_report(
        [q("E", "The diagnosis is Floppy Eyelid Syndrome. How would you manage "
                "him if he were new to you and you had just made the diagnosis?")]
    ) == []


def test_a_station_citing_nothing_at_all_is_rejected():
    problems = _written_from_the_arc_not_the_report([
        q("A", "Please examine the fundus.", drawn_from=""),
        q("B", "How would you manage her?", drawn_from=""),
    ])
    assert any("came from" in p for p in problems)


def test_one_uncited_question_does_not_sink_the_station():
    """A whole rebuild was once lost to a gate that rejected five good
    questions to punish one missing string."""
    assert _written_from_the_arc_not_the_report([
        q("A", "Please examine the fundus."),
        q("B", "How would you manage her?"),
        q("C", "What are the risk factors? Name four.", drawn_from=""),
    ]) == []


def test_an_uploaded_paper_gets_the_same_rules():
    """A newly ingested station must be built by the same builder, or the
    rules only ever apply to stations someone remembered to rebuild.

    The ingest chain queues JOB_BUILD_OSCE_PROMPTS, whose handler calls
    `build_prompts_for_station`, which is where `_arc_problems` runs. This
    asserts the chain, so detaching it fails here rather than three papers
    later.
    """
    import inspect

    from app.services.ingest import pipeline
    from app.services.osce import prompts as builder

    chain = inspect.getsource(pipeline.queue_after_ingest)
    assert "_queue_prompt_build" in chain

    queued = inspect.getsource(pipeline._queue_prompt_build)
    assert "JOB_BUILD_OSCE_PROMPTS" in queued

    handler = inspect.getsource(builder.handle_build_osce_prompts)
    assert "build_prompts_for_station" in handler

    build = inspect.getsource(builder.build_prompts_for_station)
    assert "_arc_problems" in build, (
        "the builder must run the arc checks, or an uploaded paper skips them"
    )
    assert "drawn_from" in builder.SYSTEM_PROMPT, (
        "the builder's instructions must still require each question to name "
        "what it was drawn from"
    )
