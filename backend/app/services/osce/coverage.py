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

from app.services.imagesearch.relevance import (
    MODALITY_PHRASES,
    expected_modalities,
    named_modality,
)

# Laterality as the rubric writes it. "Both eyes" is deliberately absent: a
# point about both eyes belongs in each eye's view, not a view of its own.
_RIGHT_RE = re.compile(r"\bright\s+eye\b|\bOD\b|\bRE\b|\bright\b", re.IGNORECASE)
_LEFT_RE = re.compile(r"\bleft\s+eye\b|\bOS\b|\bLE\b|\bleft\b", re.IGNORECASE)

# Beyond this a station is being padded rather than made fair, and every extra
# view is another search and another vision call. Raised from 4 when views
# began splitting by examination as well as by eye: two eyes each needing an
# external photograph and an OCT is four on its own, and the cap was silently
# dropping the rest of the rubric.
MAX_VIEWS = 6

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
    r"offers?\b|arranges?\b|orders?\b|considers?\b|manages?\b|counsels?\b|"
    # Reasoning about findings already in hand. "Correlates findings with the
    # given acuity and normal OCT report" is not something to be shown - it
    # was asking for an OCT of a normal eye.
    r"correlat\w+\b|interpret\w+\b|summaris\w+\b|summariz\w+\b|conclud\w+\b|"
    r"relates?\b|links?\b|attributes?\b|synthesis\w*\b|"
    # Answers given by saying, not by seeing. "Proposes a test to assess
    # fatiguability" wanted a photograph of an ice pack test. Recognises and
    # identifies are deliberately absent: those are usually about a sign.
    r"proposes?\b|suggests?\b|recommends?\b|lists?\b|outlines?\b|formulat\w+\b|"
    r"plans?\b|advises?\b|selects?\b|prescribes?\b|includes?\b|organis\w+\b|"
    r"organiz\w+\b|excludes?\b|investigat\w+\b|requests?\b)",
    re.IGNORECASE,
)

# An investigation the station hands over, or reports as unremarkable, is not
# one the candidate has to read. Sourcing an image of a normal OCT spends a
# search and a vision call to show nothing.
_NOTHING_TO_SEE_RE = re.compile(
    r"\bnormal\b|\bunremarkable\b|\bno\s+abnormalit\w+\b|\bwithin\s+normal\s+limits\b|"
    r"\bNAD\b|\bgiven\b|\breported\s+as\b|\bnil\s+acute\b|"
    # A point marked on absence cannot be sourced: no search returns a scan
    # showing no bony destruction. One station wanted exactly that.
    r"\bnegative\s+finding\w*\b|\babsence\s+of\b|\bno\s+evidence\s+of\b",
    re.IGNORECASE,
)


@dataclass
class View:
    """One image the station needs, and the rubric points it has to show."""

    laterality: str  # "right" | "left" | "unspecified"
    points: list[str]
    # The examination this view is, when the rubric named one. A view that
    # knows it is an OCT is searched for and verified as an OCT.
    modality: str | None = None

    @property
    def wanted_description(self) -> str:
        """What to search for and verify against, in one phrase."""
        signs = "; ".join(_strip_instruction(p) for p in self.points)
        phrase = MODALITY_PHRASES.get(self.modality or "", "")
        described = f"{phrase} showing {signs}" if phrase else signs
        if self.laterality == "unspecified":
            return described
        return f"{described} — {self.laterality} eye"


def _strip_instruction(point: str) -> str:
    """"Identify and describe microcornea in the right eye" -> "microcornea".

    The rubric is written as instructions to the marker. Searching for the
    instruction returns teaching slides about how to examine, not the sign.
    """
    # Repeated until stable: "Identifies and describes X" carries two of these,
    # and stripping the first consumes the "and", leaving "describes X".
    #
    # (?:\s|,|and)* not [\s,and]* - a character class matches the letters a, n
    # and d individually, so the pattern ate the leading d of "describe" and
    # searched for "escribe globe dystopia".
    lead = re.compile(
        r"^\s*(?:identif(?:y|ies)|describ(?:e|es)|not(?:e|es)|recognis(?:e|es)|"
        r"comment(?:s)?\s+on|mention(?:s)?)\b(?:\s|,|\band\b)*",
        re.IGNORECASE,
    )
    text = point.strip()
    while True:
        stripped = lead.sub("", text)
        if stripped == text:
            break
        text = stripped
    text = re.sub(r"^\s*(?:and\s+describes?\b|and\b)\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+in\s+the\s+(?:right|left|both)\s+eyes?\b", "", text, flags=re.IGNORECASE)
    # "the OCT shows cystoid macular oedema" -> "cystoid macular oedema". The
    # view names its own modality, so leaving this in produced search phrases
    # like "OCT showing the OCT shows ...".
    text = re.sub(
        r"^\s*(?:the\s+|a\s+|an\s+)?(?:OCT|MRI|CT|B[- ]?scan|ultrasound|angiogram|"
        r"fluorescein\s+angiogram|FFA|visual\s+field|HVF|fundus\s+(?:photograph|photo)|"
        r"slit[- ]?lamp\s+(?:photograph|photo)|external\s+(?:photograph|photo)|"
        r"topography|scan|imaging)\s+"
        r"(?:shows?|demonstrat\w+|reveals?|confirms?)\s+",
        "",
        text,
        flags=re.IGNORECASE,
    )
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
        return _by_modality("unspecified", shared)[:MAX_VIEWS]

    views: list[View] = []
    for laterality, own in sorted(grouped.items()):
        views.extend(_by_modality(laterality, own + shared))
    return views[:MAX_VIEWS]


def _by_modality(laterality: str, points: list[str]) -> list[View]:
    """Split one eye's points into the separate examinations they need.

    Grouping by eye alone was never enough. "The OCT shows intraretinal fluid"
    and "the lens is subluxed" are both the right eye, and no one photograph
    carries them: whichever image is sourced, the other point is unearnable.

    Points naming no examination get a view of their own rather than riding
    along with the named ones. That is the opposite of how an eye-less point is
    treated above, and deliberately: a general sign really is visible in both
    eyes, but a subluxed lens is not visible on an OCT. Folding the unnamed
    points into the OCT's view would report the rubric as covered while leaving
    those marks unearnable - the exact failure this module exists to prevent.
    """
    grouped: dict[str, list[str]] = {}
    plain: list[str] = []
    for point in points:
        modality = named_modality(point)
        # A point that names an investigation only to say it was normal, or
        # that it was given, is describing context rather than asking the
        # candidate to read anything. It stays with the plain view instead of
        # demanding an image of its own.
        if modality is None or _NOTHING_TO_SEE_RE.search(point):
            plain.append(point)
        else:
            grouped.setdefault(modality, []).append(point)

    views = [View(laterality, plain)] if plain else []
    views.extend(
        View(laterality, own, modality=modality)
        for modality, own in sorted(grouped.items())
    )
    return views


def station_views(station) -> list[View]:
    """Every view the station's examination tasks need, in prompt order."""
    views: list[View] = []
    for prompt in station.prompts or []:
        for view in required_views(prompt, station.findings_elicited):
            views.append(view)
            if len(views) >= MAX_VIEWS:
                return views
    return views
