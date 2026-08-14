"""The one definition of whether a station can actually be answered.

Every image failure in this bank has had the same shape. A station is built by
independent stages - write the questions, request the images, source them,
verify them, bind them to the questions that asked - and each stage was correct
about its own piece. No stage owned the finished station, so nothing ever
evaluated the only thing that matters:

    can a candidate answer every question this station asks,
    from what is actually on their screen?

That question had no owner, which is why fixing one stage moved the failure to
another, and why the audit kept agreeing with the pipeline: it was written from
the same per-stage assumptions. The audit script, the admin preview and the
sourcing selection now all read this module instead of each holding a version.

Nothing here calls a model or a search, so it is free to run on every station.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.models import OsceFigure, OsceStation
from app.services.imagesearch.relevance import (
    split_investigations,
    unsourceable_reason,
)
from app.services.osce.coverage import station_views
from app.services.osce.prompts import PRESENTS_INVESTIGATION_RE

# Below this an attached image only scraped past the gate; see station_images.
SETTLED_MATCH_CONFIDENCE = 0.78

_MISSING_NOTE_RE = re.compile(r"\[Does NOT show:\s*(.+?)\]", re.S)


@dataclass(frozen=True)
class Fault:
    """One reason a candidate could not fully answer this station."""

    kind: str
    detail: str
    # Whether searching again could fix it. False means a human must act -
    # approve something, reword a question, supply an image by hand - and
    # re-sourcing would spend and change nothing.
    fixable_by_sourcing: bool = True
    # What to search for, ready-made, for a fault that names no rubric view of
    # its own - missing_side and missing_structure exist precisely because the
    # rubric-derived view list has nothing for them, so `source_coverage_images`
    # would find nothing to source even when this fault says searching would
    # fix it. None means the existing rubric-driven sourcing already knows.
    sourcing_hint: str | None = None

    def __str__(self) -> str:  # pragma: no cover - convenience for the CLI
        return self.detail


def bound_figure_ids(prompts: list[dict]) -> set[int]:
    """Every figure that travels with a question rather than with the patient."""
    ids: set[int] = set()
    for prompt in prompts or []:
        for key in ("figure_id", "figure_ids"):
            value = prompt.get(key)
            if isinstance(value, list):
                ids.update(i for i in value if i)
            elif value:
                ids.add(value)
    return ids


def opening_figures(station: OsceStation) -> list[OsceFigure]:
    """The figures the candidate meets on entering, in order.

    A figure a question owns is not one of them: it appears when that question
    does. Counting them together made a motility station whose question C owned
    an MRI look as though it had an opening image, so no gaze montage was ever
    searched for.
    """
    claimed = bound_figure_ids(station.prompts or [])
    return sorted(
        (f for f in station.figures if f.id not in claimed), key=lambda f: f.position
    )


def answers_a_view(figure: OsceFigure) -> bool:
    """Whether this figure actually puts the view in front of the candidate.

    An image does. So do the findings stated in words: that is the last resort
    of the image protocol, reached only when every search has failed, and the
    candidate reads them on entering exactly where a photograph would have
    been. Counting images alone called 20 stations unanswerable on the same
    day the describing pass gave every one of them something to read - and,
    worse, the sitting and the audit then disagreed about the same station,
    because `sittable_prompts` has always treated stated findings as something
    shown.
    """
    if figure.image_id is not None and figure.verification_status != "rejected":
        return True
    # Unapproved words are not shown, so they answer nothing yet.
    return bool(
        (figure.described_findings or "").strip() and figure.described_findings_approved
    )


def station_faults(station: OsceStation) -> list[Fault]:
    """Every reason this station's marks cannot currently be earned."""
    faults: list[Fault] = []
    views = station_views(station)
    # A rejected figure is a decision already taken, not a decision outstanding.
    # Counting it as "not approved" made a station carrying three refused images
    # report three faults that no one could act on, and inflated the audit with
    # work that does not exist.
    opening = [f for f in opening_figures(station) if answers_a_view(f)]

    # --- What the candidate opens on ------------------------------------
    if views and not opening:
        faults.append(Fault(
            "no_opening_image",
            f"no image at all, and the rubric needs {len(views)}",
        ))
    elif len(opening) < len(views):
        faults.append(Fault(
            "too_few_views",
            f"{len(opening)} image(s) for {len(views)} view(s) the rubric needs",
        ))

    seen_images: dict[int, int] = {}
    for figure in opening:
        label = f"figure {figure.position}"
        # Everything below judges a photograph - whether it is the right one,
        # whether it repeats another, whether anyone has approved it. A view
        # answered in words has no photograph to judge, and the description was
        # written and published under the leak guard rather than the image one.
        if figure.image_id is None:
            continue
        # The same photograph attached twice is one view shown twice, which is
        # what a rubric split into technique marks used to produce.
        if figure.image_id in seen_images:
            faults.append(Fault(
                "duplicate_image",
                f"{label} repeats the image already shown as figure "
                f"{seen_images[figure.image_id]}",
                fixable_by_sourcing=False,
            ))
        else:
            seen_images[figure.image_id] = figure.position

        if figure.verification_status == "representative":
            missing = _MISSING_NOTE_RE.search(figure.verification_notes or "")
            faults.append(Fault(
                "representative_only",
                f"{label} is representative only"
                + (f": missing {missing.group(1).strip()[:90]}" if missing else ""),
            ))
        elif (figure.match_confidence or 1.0) < SETTLED_MATCH_CONFIDENCE:
            faults.append(Fault(
                "low_confidence",
                f"{label} scraped in at {figure.match_confidence:.0%} confidence",
            ))
        if not figure.is_approved:
            faults.append(Fault(
                "not_approved",
                f"{label} is not approved, so nothing is shown for it",
                fixable_by_sourcing=False,
            ))

    # --- What each question asks the candidate to read -------------------
    for prompt in station.prompts or []:
        label = prompt.get("label") or "?"
        wanted = str(prompt.get("image_wanted") or "").strip()
        text = str(prompt.get("text") or "")
        bound = bound_figure_ids([prompt])

        # A question that hands something over must have asked for it. This is
        # the invariant the pipeline never had, checked here as well as at the
        # point the question is written, because stations built before the rule
        # existed still carry the fault.
        if PRESENTS_INVESTIGATION_RE.search(text) and not wanted and not bound:
            faults.append(Fault(
                "presents_nothing",
                f"question {label} presents an investigation but never asked for "
                f"one, so it shows a blank screen",
                fixable_by_sourcing=False,
            ))
            continue

        if not wanted or bound:
            continue

        # A question the reconcile pass has already restated asks for nothing
        # the candidate cannot answer without. Station 238 now reads "what
        # would you expect a cerebral angiogram to show", and 213 states its
        # Gram stain result in words - both stand on their own, and showing the
        # picture would hand over the mark. The request survives only as a
        # matching key, so the ingested binder can still give the question a
        # figure the examiners' own report holds; it is no longer a promise to
        # anybody. Reporting it as a missing image asked for work that would
        # damage the station if it were ever done.
        #
        # Only while the wording keeps its side of that bargain. A question
        # that says "this is the angiogram" is owed one however exhausted the
        # search is, so it falls through to the fault below.
        if prompt.get("image_search_exhausted") and not PRESENTS_INVESTIGATION_RE.search(text):
            continue

        impossible = unsourceable_reason(wanted)
        if impossible:
            faults.append(Fault(
                "impossible_request",
                f"question {label} wants '{wanted[:40]}', which is {impossible} - "
                f"reword it rather than re-sourcing",
                fixable_by_sourcing=False,
            ))
        else:
            count = len(split_investigations(wanted))
            faults.append(Fault(
                "missing_investigation",
                f"question {label} has no image for its investigation"
                + (f" ({count} investigations asked for)" if count > 1 else ""),
            ))

    faults.extend(_wrong_eye(station))
    faults.extend(_answers_itself(station))
    faults.extend(_missing_side(station))
    faults.extend(_missing_structure(station))
    return faults


_BOTH_RE = re.compile(r"\bboth\s+eyes\b|\bbilateral(?:ly)?\b", re.IGNORECASE)


def _missing_side(station: OsceStation) -> list[Fault]:
    """Both eyes asked for, and every image shown is of the same one.

    `_wrong_eye` deliberately lets "both eyes" through against any single-eye
    caption, because asking for both is not a contradiction of showing one.
    But two images that both name the RIGHT eye is not "both eyes" either -
    station 317 opened two different photographs, both captioned "Fundus
    photograph of the right eye", against a step 1 that asked to examine the
    fundus of both eyes. No caption disagreed with the question, so nothing
    caught it; the left eye was simply never shown.
    """
    from app.services.osce.prompts import VIEW_MODALITIES
    from app.services.osce.station_images.ingested import _named_eyes

    # Only images of the eye itself decide this, not the investigations beside
    # them. Station 317 also carried a right-eye and a left-eye visual field,
    # and pooling those in made the two identical right-eye fundus photographs
    # look like "both eyes shown" - the fields answered a different question.
    opening = [
        f for f in opening_figures(station)
        if answers_a_view(f) and (f.modality or "") in VIEW_MODALITIES
    ]
    named = [_named_eyes(f.caption) for f in opening]
    if not named:
        return []
    # A composite or unlabelled image ("of both eyes", or no side at all) may
    # be carrying the missing side. Firing only when EVERY opening image
    # commits to exactly one side, and they all agree, keeps this to the
    # unambiguous case: two named-right fundus photographs and nothing else is
    # a real gap, but one named-left photo next to an uncaptioned composite is
    # not something this can call - the composite may already be the right
    # eye, and a guard that strips a usable station on a guess is worse than
    # the leak it was trying to catch.
    if any(len(n) != 1 for n in named):
        return []
    sides_shown: set[str] = set().union(*named)
    if sides_shown == {"left", "right"}:
        return []

    # What to ask for: the same kind of photograph the shown side already has.
    modality_label = {
        "fundus": "Fundus photograph",
        "external": "External photograph",
        "slit_lamp": "Slit lamp photograph",
        "motility": "External photograph",
        "orthoptic": "External photograph",
        "photo": "Photograph",
    }.get(opening[0].modality or "", "Photograph")
    missing = "left" if sides_shown == {"right"} else "right"

    faults: list[Fault] = []
    for prompt in station.prompts or []:
        if prompt.get("step") != 1:
            continue
        text = str(prompt.get("text") or "")
        if not _BOTH_RE.search(text):
            continue
        faults.append(Fault(
            "missing_side",
            f"question {prompt.get('label') or '?'} asks to examine both eyes, "
            f"but every image that names a side names the {next(iter(sides_shown))} "
            f"eye - no {missing} eye is shown",
            sourcing_hint=f"{modality_label} of the {missing} eye",
        ))
    return faults


_ANTERIOR_RE = re.compile(
    r"\banterior\s+segment\b|\bcornea\b|\bconjunctiva\b|\biris\b|\blens\b|"
    r"\banterior\s+chamber\b|\beyelids?\b",
    re.IGNORECASE,
)
_POSTERIOR_RE = re.compile(
    r"\bposterior\s+segment\b|\bfundus\b|\bretina\b|\boptic\s+(?:nerve|disc)\b|"
    r"\bmacula\b|\bvitreous\b",
    re.IGNORECASE,
)
_ANTERIOR_MODALITIES = {"external", "slit_lamp"}
_POSTERIOR_MODALITIES = {"fundus"}
# An OCT captioned plainly, or of "the retina"/"the macula", is a posterior
# scan by convention - anterior segment OCT always says so. Treating every OCT
# as ambiguous flagged four stations whose only fault was a caption that
# didn't repeat the word "macula", including one carrying a real anterior
# segment OCT correctly labelled as such right next to it.
_OCT_ANTERIOR_RE = re.compile(
    r"\banterior\s+segment\b|\banterior\s+chamber\b|\bAS[- ]?OCT\b", re.IGNORECASE
)


def _oct_covers(caption: str | None) -> str:
    """Which structure a plain OCT figure counts toward."""
    return "anterior" if _OCT_ANTERIOR_RE.search(caption or "") else "posterior"


def _missing_structure(station: OsceStation) -> list[Fault]:
    """Asked to examine a structure no image on screen can show.

    Binary "is there any view at all" passed a step 1 asking to "examine the
    anterior segment and fundus of both eyes" against a station whose only
    images were two fundus photographs - there was a view, so the six marks
    on the anterior segment half were unanswerable to anyone. The structure
    named in the question has to have its own kind of image, not just any.
    """
    opening = [f for f in opening_figures(station) if answers_a_view(f) and f.image_id]
    if not opening:
        return []
    covers_anterior = any(
        f.modality in _ANTERIOR_MODALITIES
        or (f.modality == "oct" and _oct_covers(f.caption) == "anterior")
        for f in opening
    )
    covers_posterior = any(
        f.modality in _POSTERIOR_MODALITIES
        or (f.modality == "oct" and _oct_covers(f.caption) == "posterior")
        for f in opening
    )

    from app.services.osce.station_images.ingested import _named_eyes

    faults: list[Fault] = []
    modalities = sorted({f.modality or "unlabelled" for f in opening})
    for prompt in station.prompts or []:
        if prompt.get("step") != 1:
            continue
        text = str(prompt.get("text") or "")
        if not re.search(r"\bexamin\w*\b", text, re.IGNORECASE):
            continue
        label = prompt.get("label") or "?"
        side = _named_eyes(text)
        of_eye = (
            f"the {next(iter(side))} eye" if len(side) == 1 else "both eyes"
        )
        if _ANTERIOR_RE.search(text) and not covers_anterior:
            faults.append(Fault(
                "missing_structure",
                f"question {label} asks to examine the anterior segment, but no "
                f"external, slit-lamp or anterior-segment OCT image is shown - "
                f"only {modalities}",
                sourcing_hint=f"External or slit lamp photograph of the anterior "
                f"segment of {of_eye}",
            ))
        if _POSTERIOR_RE.search(text) and not covers_posterior:
            faults.append(Fault(
                "missing_structure",
                f"question {label} asks to examine the fundus/posterior segment, "
                f"but no fundus photograph or posterior OCT is shown - "
                f"only {modalities}",
                sourcing_hint=f"Fundus photograph of {of_eye}",
            ))
    return faults


def _wrong_eye(station: OsceStation) -> list[Fault]:
    """A question about one eye showing a figure captioned the other.

    Worse than showing nothing: the candidate reads the picture correctly and
    is marked wrong for it. Four of these were live, and one was created by the
    binder itself in the course of fixing something else - which is why the
    check belongs here, where every stage's output is judged, and not inside
    any one stage.
    """
    from app.services.osce.station_images.ingested import _opposite_eyes

    by_id = {f.id: f for f in station.figures}
    faults: list[Fault] = []
    for prompt in station.prompts or []:
        text = str(prompt.get("text") or "")
        for fid in bound_figure_ids([prompt]):
            figure = by_id.get(fid)
            if figure is not None and _opposite_eyes(text, figure.caption):
                faults.append(Fault(
                    "wrong_eye",
                    f"question {prompt.get('label') or '?'} asks about one eye "
                    f"and shows {(figure.caption or 'a figure')[:40]!r}",
                    fixable_by_sourcing=False,
                ))
    return faults


def _answers_itself(station: OsceStation) -> list[Fault]:
    """A question that gives away what it was set to test.

    Every repair pass so far has been capable of introducing this while fixing
    something else - the differential fix made the model assert the finding,
    and the words-for-images pass named the diagnosis in question A eleven
    times. A repair that damages the station is a fault like any other, so it
    is judged in the same place rather than guarded separately inside each pass.
    """
    from app.services.osce.reconcile import _states_more_than_it_asks

    faults: list[Fault] = []
    for prompt in station.prompts or []:
        step = int(prompt.get("step") or 0)
        # The standing instruction has to name the region it sends the
        # candidate to. "Please examine the left upper eyelid" was flagged
        # against a diagnosis of left upper eyelid carcinoma, and there is no
        # wording that asks the question without it.
        if step == 1:
            continue
        why = _states_more_than_it_asks(
            str(prompt.get("text") or ""), station, step < 5,
        )
        if why:
            faults.append(Fault(
                "answers_itself",
                f"question {prompt.get('label') or '?'} {why}",
                fixable_by_sourcing=False,
            ))
    return faults


def is_sittable(station: OsceStation) -> bool:
    """Whether a candidate could answer this station in full, today."""
    return not station_faults(station)


def stations_worth_sourcing(stations: list[OsceStation]) -> list[int]:
    """Those a search could still help - not those waiting on a person."""
    return [
        s.id
        for s in stations
        if any(f.fixable_by_sourcing for f in station_faults(s))
    ]
