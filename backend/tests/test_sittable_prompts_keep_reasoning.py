"""Only an instruction to look may be dropped, never a question about it.

Station 87 served the candidate two questions. It holds four. Its A - "Please
describe the findings related to the patient's pupils and eye movements" - was
dropped correctly, because the station has no image and states those findings
on entry. Its B - "What is your differential diagnosis for these findings, and
how would you confirm your leading diagnosis?" - was dropped too, on the bare
word "findings", and with it 6.5 of the station's 20 marks. What reached the
candidate opened on "The diagnosis is Adie's pupil".

`_EXAMINE_RE` was written to decide which questions EARN AN IMAGE and is
deliberately broad for that. Deleting questions needs the opposite.
"""

from __future__ import annotations

from app.services.osce.coverage import sittable_prompts


class Figure:
    def __init__(self, described=True):
        self.id = 1
        self.image_id = None
        self.is_approved = False
        self.described_findings = "The left pupil is dilated." if described else None
        self.described_findings_approved = described


class Station:
    def __init__(self, prompts, figures=None):
        self.prompts = prompts
        self.figures = figures if figures is not None else [Figure()]


def q(label, text, marks=5.0, seconds=120):
    return {"label": label, "text": text, "seconds": seconds,
            "rubric": [{"text": "point", "marks": marks}]}


def labels(station):
    return [str(p.get("label")) for p in sittable_prompts(station)]


def test_the_differential_survives_a_station_with_no_image():
    station = Station([
        q("A", "Please describe the findings related to the patient's pupils."),
        q("B", "What is your differential diagnosis for these findings, and how "
               "would you confirm your leading diagnosis?"),
        q("C", "The diagnosis is Adie's pupil. Explain the pathophysiology."),
        q("D", "What are some other causes of light-near dissociation?"),
    ])
    assert labels(station) == ["B", "C", "D"]


def test_a_pure_instruction_to_look_is_still_dropped():
    station = Station([
        q("A", "Please examine the ocular motility of both eyes."),
        q("B", "How would you manage this patient?"),
    ])
    assert labels(station) == ["B"]


def test_what_else_would_you_examine_is_a_question_not_an_instruction():
    """Station 204 lost this one. The printed findings do not answer it."""
    station = Station([
        q("A", "Please examine the anterior segment and describe your findings."),
        q("B", "What else would you examine in this patient?"),
        q("C", "How would you manage her?"),
    ])
    assert labels(station) == ["B", "C"]


def test_summarise_and_investigate_are_not_examinations():
    for text in (
        "Summarise your findings and give me three differential diagnoses.",
        "What investigations would you order given these findings?",
        "What are the risk factors for the findings you have described?",
    ):
        station = Station([q("A", text), q("B", "How would you manage her?")])
        assert labels(station) == ["A", "B"], text


def test_a_station_with_an_image_keeps_every_question():
    class WithImage(Figure):
        def __init__(self):
            super().__init__(described=False)
            self.image_id, self.is_approved = 9, True

    station = Station(
        [q("A", "Please examine and describe your findings."), q("B", "Manage her.")],
        figures=[WithImage()],
    )
    assert labels(station) == ["A", "B"]
