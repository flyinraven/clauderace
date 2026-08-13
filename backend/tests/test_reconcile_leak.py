"""A question rewritten to carry its finding in words must not answer itself.

Thirteen questions came back from the words-for-images pass stating the result
and then asking the candidate to describe it, and one opened by naming the
diagnosis the station reveals two questions later. Both hand over the marks the
question was set to earn.
"""

from __future__ import annotations

from app.models import OsceStation
from app.services.osce.reconcile import _states_more_than_it_asks


def station(**kw) -> OsceStation:
    base = dict(
        diagnosis="Multifocal choroiditis with a right choroidal neovascular membrane",
        findings_elicited="Right macular scar, intraretinal fluid",
        findings=None,
        aims=None,
        rubric=None,
        case_summary=None,
    )
    base.update(kw)
    return OsceStation(**base)


def test_naming_the_diagnosis_is_refused():
    s = station()
    why = _states_more_than_it_asks(
        "This patient has a history of multifocal choroiditis with a right "
        "choroidal neovascular membrane. What would you expect the OCT to show?",
        s,
    )
    assert why is not None
    assert "diagnosis" in why


def test_stating_the_result_then_asking_for_it_is_refused():
    s = station()
    # Wording drawn from the findings, not the diagnosis, so only the second
    # rule can be what rejects it.
    why = _states_more_than_it_asks(
        "Her OCT shows intraretinal fluid at the fovea. Talk me through what "
        "it shows.",
        s,
    )
    assert why is not None
    assert "describe" in why


def test_stating_the_result_then_asking_what_it_means_is_kept():
    s = station()
    assert _states_more_than_it_asks(
        "Her OCT shows intraretinal fluid at the fovea. What does that tell "
        "you, and how would it change your management?",
        s,
    ) is None


def test_the_expected_form_is_always_safe():
    s = station()
    assert _states_more_than_it_asks(
        "What would you expect her fluorescein angiogram to show, and how "
        "would it change your management?",
        s,
    ) is None


def test_an_ordinary_question_handing_over_a_picture_is_untouched():
    """"This is her OCT. Describe what it shows" states no finding - it is the
    normal wording for a question that really has a picture."""
    s = station()
    assert _states_more_than_it_asks(
        "This is her OCT. Describe what it shows.", s
    ) is None


def test_the_reveal_question_may_name_the_diagnosis():
    """"The diagnosis is a third nerve palsy. What would you expect her CT
    angiography to show?" is the reveal doing its job. The first version of
    this guard rejected it, which is the same over-firing that has rejected
    good stations all week."""
    s = station(diagnosis="Third nerve palsy")
    text = ("The diagnosis is a third nerve palsy. What would you expect her CT "
            "angiography to show, and how would it change your management?")
    assert _states_more_than_it_asks(text, s, before_the_reveal=False) is None
    assert _states_more_than_it_asks(text, s, before_the_reveal=True) is not None
