"""Shared by more than one of the OSCE route modules."""

from __future__ import annotations

import re
from typing import Any
from app.api.deps import DbSession, load_owned
from app.models import OsceSession
from app.services.imagesearch.relevance import is_investigation, is_the_patient
from app.services.osce.circuit import compute_station_clock


# A single spoken answer. Generous enough for a rambling 3-minute reply,
# tight enough to reject a runaway recording.
MAX_AUDIO_BYTES = 15 * 1024 * 1024
ACCEPTED_AUDIO_PREFIXES = ("audio/", "video/mp4", "video/webm")


def _all_bound_ids(prompts: list[dict[str, Any]]) -> set[int]:
    """Every figure that travels with a question rather than with the patient."""
    return {i for p in prompts for i in _bound_figure_ids(p)}


def _bound_figure_ids(prompt: dict[str, Any]) -> list[int]:
    """The figures this question carries, in order, first one first.

    A question asking for two investigations holds `figure_ids`; one asking for
    a single image holds only `figure_id`, which is every station written before
    the list existed. `figure_id` is kept as the first of the list, so reading
    both and de-duplicating is what covers all of them.
    """
    ids = [i for i in (prompt.get("figure_ids") or []) if i]
    first = prompt.get("figure_id")
    if first and first not in ids:
        ids.insert(0, first)
    return ids


def _load_sitting(db: DbSession, session_id: int, user) -> OsceSession:
    return load_owned(db, OsceSession, session_id, user)


def _clock(sitting: OsceSession):
    return compute_station_clock(
        sitting.started_at, sitting.submitted_at, sitting.is_timed
    )


# Words that carry no clinical weight, so overlap between two sentences built
# only from these means nothing.
_EMPTY = frozenset("""the a an and or of in on for with to this that these those his her their its
is are was were be been being as at by from into over under not no any all both each patient
eye eyes left right bilateral there here also well""".split())


def _content(text: str) -> set[str]:
    """Comparable words. Decimals and fractions survive whole - "6/4.8" and
    "0.5" are findings - but a sentence-final full stop is not part of the
    word, and leaving it attached made "eye." fail to match "eye", which
    dropped a stated intraocular pressure the examiner is meant to give."""
    out = set()
    for word in re.findall(r"[a-z0-9/.]{3,}", (text or "").lower()):
        word = word.strip(".")
        # A light plural so "Intraocular pressures are 14 and 17" still counts
        # as the "Intraocular pressure Right 14 mmHg" the station handed over.
        # Only above four letters: "lash" must not become "las" and start
        # matching things it has nothing to do with.
        if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        if len(word) >= 3 and word not in _EMPTY:
            out.add(word)
    return out


def _given_only(figure) -> str | None:
    """The half of a spoken description the examiner is allowed to hand over.

    `described_findings` was written as the last resort for a figure with no
    image: the examiner says what the investigation showed. When an image did
    arrive it is neither - it is the answer, printed under the picture that was
    supposed to be read for it. Station 623 showed a nine-gaze montage and
    below it "There is a left hypertropia of 10 prism diopters... a positive
    3-step test... mild left inferior oblique overaction... an abnormal head
    posture", which is question A's four critical marks in the examiner's own
    words. 461 figures across 228 stations were doing this.

    Not all of it is a leak. A real examiner states the acuity and the pressure
    and expects the candidate to elicit the signs, which is the split the
    station already carries as `findings_given` and `findings_elicited`. So the
    sentences grounded in `findings_given` stay and the rest go, rather than
    blanking the panel and taking the acuities away with it.

    Where the station never recorded a split, nothing is grounded and the whole
    panel goes. That is the safe direction: the image is still on screen, so no
    candidate is left with nothing to read.
    """
    described = figure.described_findings or ""
    station = figure.station
    given = _content(getattr(station, "findings_given", None) or "")
    if not given:
        return None
    kept = []
    for sentence in re.split(r"(?<=[.!?])\s+", described):
        words = _content(sentence)
        if not words:
            continue
        # Grounded means the sentence says nothing the station did not already
        # hand over - every content word of it appears in `findings_given`.
        # Tolerating a single stray word was tried and is too loose: "There is
        # lash ptosis." differs from a given history of "progressive ptosis" by
        # the one word "lash", and that word is the three-mark critical item.
        if not (words - given):
            kept.append(sentence.strip())
    return " ".join(kept) or None


def visible_figure(figure) -> dict[str, Any] | None:
    """One figure as the candidate may see it, or None if they may not.

    Two ways a figure reaches them, and a figure with neither reaches them not
    at all - a search that found nothing leaves a row behind, and showing it
    would put an empty frame on the screen.

      - an image that was attached and approved;
      - the protocol's last resort: no image could be found, so the examiner
        states the findings and the candidate reads those instead.

    A rejected figure is neither, so it is never returned. That is the point of
    rejecting it.

    Shared by the sitting and the review deliberately. They showed the same
    thing by two copies of this rule until the review needed images too, and a
    review that showed a figure the sitting withheld would be marking the
    candidate against something they never saw.
    """
    shows_image = bool(figure.image_id and figure.is_approved)
    described = (
        figure.described_findings if figure.described_findings_approved else None
    )
    # A picture and the findings printed under it is the answer given away.
    # See `_given_only`: the examiner keeps what he would really state and
    # stops speaking the signs the candidate is being marked on finding.
    if shows_image and described:
        described = _given_only(figure)
    if not shows_image and not described:
        return None
    return {
        "id": figure.id,
        "image_id": figure.image_id if shows_image else None,
        "caption": figure.caption if shows_image else None,
        "described_findings": described,
        "position": figure.position,
    }


def figures_for_prompt(by_id: dict[int, Any], prompt: dict[str, Any]) -> list[dict[str, Any]]:
    """The investigations one question asks the candidate to read."""
    out = []
    for figure_id in _bound_figure_ids(prompt):
        figure = by_id.get(figure_id)
        if figure is None:
            continue
        payload = visible_figure(figure)
        if payload:
            out.append(payload)
    return out


def opening_figures_payload(station) -> list[dict[str, Any]]:
    """What is on screen from the start: the patient, and nothing more.

    An image belonging to a question is not shown with the patient - an MRI on
    screen from the beginning answers the question before it is asked.

    Nor is an investigation that no question claimed. At a real station the
    candidate sees the patient and asks for the rest: "How would you confirm
    the diagnosis?" earns the Pentacam. Showing the printouts from the start
    inverts that - it hands over the answer and buries the view the rubric was
    actually written for. Station 155 opened on four corneal topography maps
    and one slit lamp photograph captioned "of one eye", and its eight marks
    for the graft and the apical scar could not be earned from any of them.

    A station whose every image is an investigation would be left with a blank
    screen, and blank is worse than early: those keep what they have, and the
    audit reports them as having no view of the patient.
    """
    owned = {i for p in (station.prompts or []) for i in _bound_figure_ids(p)}
    shown, held_back = [], []
    for figure in sorted(station.figures, key=lambda f: f.position):
        if figure.id in owned:
            continue
        payload = visible_figure(figure)
        if not payload:
            continue
        # Words are the examiner speaking, never an investigation.
        # The test is "do we know this is the patient", not "do we know this is
        # an investigation": an unnamed modality answers no to both, and the
        # two mistakes are not symmetrical. Station 312 opened on an IOL
        # calculation printout and two specular microscopy images, all recorded
        # as "other", before the candidate had been asked to examine anything.
        if figure.image_id and not is_the_patient(figure.modality):
            held_back.append(payload)
        else:
            shown.append(payload)
    return shown or held_back
