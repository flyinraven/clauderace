"""Which images a station needs before its marks can actually be earned.

A station is marked on what the candidate describes. If the rubric awards marks
for a Baerveldt tube in the left eye and the candidate is shown one photograph
of the right eye, those marks are unearnable no matter how good the answer —
the station is not hard, it is impossible.

So the images are chosen to cover the rubric rather than to illustrate the
case. The rubric is grouped into the views a real examiner would put in front
of the candidate — one per eye, per modality — and each view is sourced
separately.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.imagesearch.relevance import expected_modalities

# Laterality as the rubric writes it. "Both eyes" is deliberately absent: a
# point about both eyes belongs in each eye's view, not a view of its own.
_RIGHT_RE = re.compile(r"\bright\s+eye\b|\bOD\b|\bRE\b|\bright\b", re.IGNORECASE)
_LEFT_RE = re.compile(r"\bleft\s+eye\b|\bOS\b|\bLE\b|\bleft\b", re.IGNORECASE)

# Beyond this a station is being padded rather than made fair, and every extra
# view is another search and another vision call.
MAX_VIEWS = 4

# A task earns images by asking the candidate to look at something. Mentioning
# a structure is not enough: "history in a patient with congenital cataracts"
# names the lens but is answered from the history, not from a photograph.
_EXAMINE_RE = re.compile(
    r"\bexamin\w+\b|\binspect\w*\b|\blook\s+at\b|\bdescribe\b|\bwhat\s+(?:do\s+you\s+see|"
    r"does\s+(?:this|the\s+image)\s+show)\b|\bfindings?\b|\bshown\b|\bthis\s+(?:photograph|"
    r"image|scan|OCT|angiogram)\b|\binterpret\b|\breport\s+(?:this|the)\b",
    re.IGNORECASE,
)

# Rubric points the candidate answers by talking, not by seeing. They must not
# drive an image search, and they are not marks an image can unlock.
_NON_VISUAL_RE = re.compile(
    r"^\s*(?:asks?\b|enquir\w+\b|elicits?\b|states?\b|explains?\b|discusses?\b|"
    r"offers?\b|arranges?\b|orders?\b|considers?\b|manages?\b|counsels?\b)",
    re.IGNORECASE,
)


@dataclass
class View:
    """One image the station needs, and the rubric points it has to show."""

    laterality: str  # "right" | "left" | "unspecified"
    points: list[str]

    @property
    def wanted_description(self) -> str:
        """What to search for and verify against, in one phrase."""
        signs = "; ".join(_strip_instruction(p) for p in self.points)
        if self.laterality == "unspecified":
            return signs
        return f"{signs} — {self.laterality} eye"


def _strip_instruction(point: str) -> str:
    """"Identify and describe microcornea in the right eye" -> "microcornea".

    The rubric is written as instructions to the marker. Searching for the
    instruction returns teaching slides about how to examine, not the sign.
    """
    text = re.sub(
        r"^\s*(?:identif(?:y|ies)|describ(?:e|es)|not(?:e|es)|recognis(?:e|es)|"
        r"comment(?:s)?\s+on|mention(?:s)?)\b[\s,and]*",
        "",
        point.strip(),
        flags=re.IGNORECASE,
    )
    text = re.sub(r"^\s*(?:and\s+describes?\b|and\b)\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+in\s+the\s+(?:right|left|both)\s+eyes?\b", "", text, flags=re.IGNORECASE)
    return text.strip(" .,;") or point.strip()


def _laterality(text: str) -> str:
    right, left = bool(_RIGHT_RE.search(text)), bool(_LEFT_RE.search(text))
    if right and not left:
        return "right"
    if left and not right:
        return "left"
    return "unspecified"


def required_views(prompt: dict, station_findings: str | None = None) -> list[View]:
    """The views one task needs, grouped from its rubric.

    Only tasks that ask the candidate to *look* generate views: a question
    about management is answered from the history, and sourcing an image for
    it wastes a search and clutters the station.
    """
    text = str(prompt.get("text") or "")
    if not _EXAMINE_RE.search(text):
        return []

    rubric = prompt.get("rubric") or []
    points = [
        str(p.get("text") if isinstance(p, dict) else p).strip()
        for p in rubric
        if str(p.get("text") if isinstance(p, dict) else p).strip()
        and not _NON_VISUAL_RE.match(str(p.get("text") if isinstance(p, dict) else p).strip())
    ]
    if not points:
        return []

    # A task with no examinable wording needs no image, however long its rubric.
    if not expected_modalities(text, " ".join(points)):
        return []

    grouped: dict[str, list[str]] = {}
    for point in points:
        grouped.setdefault(_laterality(point), []).append(point)

    # Points that never named an eye belong with every eye that did, otherwise
    # a general sign becomes a view of its own and doubles the searching.
    shared = grouped.pop("unspecified", [])
    if not grouped:
        return [View("unspecified", shared)][:MAX_VIEWS]

    views = [
        View(laterality, points + shared)
        for laterality, points in sorted(grouped.items())
    ]
    return views[:MAX_VIEWS]


def station_views(station) -> list[View]:
    """Every view the station's examination tasks need, in prompt order."""
    views: list[View] = []
    for prompt in station.prompts or []:
        for view in required_views(prompt, station.findings_elicited):
            views.append(view)
            if len(views) >= MAX_VIEWS:
                return views
    return views
