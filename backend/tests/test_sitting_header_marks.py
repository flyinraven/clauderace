"""The page must say what it is going to ask for.

Station 341 asks for 9.5 marks - its 10.5-mark examination question is dropped,
because the station has no image and prints its findings on entry - and headed
the page "20 marks". A perfect answer looked like half a fail. Marking already
worked off what was actually asked; only this line lied.
"""

from __future__ import annotations

from app.services.osce.coverage import sittable_prompts


class Figure:
    id = 1
    image_id = None
    is_approved = False
    described_findings = "There is globe retraction on adduction."
    described_findings_approved = True


class Station:
    def __init__(self, prompts):
        self.prompts = prompts
        self.figures = [Figure()]
        self.total_marks = 20


def q(label, text, marks, seconds=120):
    return {"label": label, "text": text, "seconds": seconds,
            "rubric": [{"text": "point", "marks": marks}]}


def served_marks(station) -> float:
    """What the sitting endpoint now puts in the header."""
    return sum(
        sum(float(r.get("marks") or 0) for r in (p.get("rubric") or []))
        for p in sittable_prompts(station)
    )


def test_a_dropped_examination_is_not_counted_in_the_header():
    station = Station([
        q("A", "Please examine the ocular motility of both eyes.", 10.5),
        q("B", "What would you do to confirm the globe retraction?", 1),
        q("C", "Summarise your findings and give three differentials.", 5.5),
        q("D", "What are the different types of Duane's syndrome?", 3),
    ])
    assert served_marks(station) == 9.5


def test_a_station_that_asks_everything_still_reads_twenty():
    station = Station([
        q("A", "How would you manage her?", 12),
        q("B", "What are the systemic associations?", 8),
    ])
    assert served_marks(station) == 20
