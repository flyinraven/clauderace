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


def station_faults(station: OsceStation) -> list[Fault]:
    """Every reason this station's marks cannot currently be earned."""
    faults: list[Fault] = []
    views = station_views(station)
    # A rejected figure is a decision already taken, not a decision outstanding.
    # Counting it as "not approved" made a station carrying three refused images
    # report three faults that no one could act on, and inflated the audit with
    # work that does not exist.
    opening = [
        f
        for f in opening_figures(station)
        if f.image_id is not None and f.verification_status != "rejected"
    ]

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
