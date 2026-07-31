"""Source and verify clinical images for OSCE stations.

The real OSCE puts a live patient in front of the candidate. A station with no
image cannot test visual recognition, so images are searched for using the
station's own findings - but a generic web photograph of "hypermature cataract"
will not show *this* patient's inferior subluxation. An image that contradicts
the rubric is worse than none, because the candidate is then marked down for
correctly describing what they can actually see.

Every candidate image is therefore checked by a vision model against the
station's elicited findings, and rejected unless it genuinely shows them.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.models import Image, OsceFigure, OsceStation
from app.services.ai import AIClient, AIError, ImagePart, TextPart
from app.services.coerce import as_float
from app.services.errors import log_error
from app.services.imagesearch.base import ImageQueryError, ImageSearchError
from app.services.imagesearch.relevance import (
    expected_modalities,
    modality_mismatch,
    named_modality,
    split_investigations,
    unsourceable_reason,
    wants_gaze_positions,
)
from app.services.imagesearch.service import (
    attach_image_to_figure,
    build_provider,
    check_and_consume_quota,
    download_candidate,
)
from app.services.jobs.runner import JobContext, JobHandlerError, register_handler
from app.services.osce.coverage import station_views
from app.services.settings_store import SettingsStore

logger = logging.getLogger(__name__)

JOB_SOURCE_STATION_IMAGES = "source_station_images"

# Below this a faithful match is not trustworthy enough to show as the patient.
MIN_MATCH_CONFIDENCE = 0.7
# A representative image only has to be a genuine, describable clinical image of
# the right pathology, so it clears a lower bar - but it is labelled as such.
MIN_REPRESENTATIVE_CONFIDENCE = 0.55
# Above this an attached image is left alone by a batch re-source. It sits above
# `MIN_MATCH_CONFIDENCE` deliberately: a picture that only scraped past the
# attachment gate is worth one more search, whereas re-buying a confident one
# costs a Brave call and a vision call to arrive back where it started.
SETTLED_MATCH_CONFIDENCE = 0.78

QUERY_SYSTEM = """\
You write image search queries for ophthalmology teaching material.

Given an OSCE station's case and the clinical signs the candidate is meant to
see, write THREE search phrases, from most specific to most general. Searching
only for the full picture fails on stations that combine several findings, but
the underlying pathologies are common and easy to find on their own.

  1. specific  - modality plus the distinctive combination of signs
  2. core      - modality plus the single main diagnosis
  3. broad     - the diagnosis or classic sign alone, no modality

Name the modality as a photographer would: "slit lamp photograph", "fundus
photograph", "external eye photograph", "OCT", "fluorescein angiogram".

If the signs are a motility, gaze, squint or cranial nerve palsy deficit, keep
"nine positions of gaze" in phrases 1 and 2: the deficit is only visible across
the positions, and a single frontal photograph shows nothing to describe.

Never include incidental surgical hardware, laterality, or a second unrelated
condition in phrases 2 and 3 - those are what make a search return nothing.

Return ONLY a JSON object:
{"queries": ["specific phrase", "core phrase", "broad phrase"]}"""

VERIFY_SYSTEM = """\
You are checking whether a photograph is suitable to show a candidate at an
ophthalmology OSCE station, where they will be asked to describe what they see.

You are given the station's clinical signs and one candidate image. Grade it
into one of three tiers.

"faithful"        - a genuine clinical image of the right modality that shows
                    the described signs. Ideal: the candidate can be marked
                    against the station's rubric as written.
"representative"  - a genuine clinical image of the right modality showing the
                    station's core pathology, but not every stated sign (for
                    example the right disease without the specific laterality,
                    severity, or an incidental surgical device). Still valuable
                    teaching material.
"reject"          - anything else.

ALWAYS reject:
  - diagrams, illustrations, cartoons, graphs, slides, tables
  - arrows, circles, asterisks or text annotations that point out the
    abnormality, since they do the candidate's describing for them
  - burned-in captions that name the diagnosis or the sign
  - watermarks, clinic branding, before-and-after marketing images
  - veterinary or non-human eyes
  - the wrong modality entirely (an OCT when an external photo is needed)
  - a different disease, or quality too poor to describe

DO NOT reject a multi-panel image merely for being multi-panel. A montage of
the nine positions of gaze is the standard, and often the only, way to
photograph ocular motility, cranial nerve palsies, Duane's syndrome and lid
disorders - such a montage is exactly what a candidate should be shown. Plain
panel letters with no explanatory text are acceptable; it is annotation that
identifies the abnormality which is not.

If the signs describe ocular motility - a duction or muscle deficit, a gaze
palsy, a squint, a cranial nerve palsy, nystagmus - the image MUST show more
than one position of gaze. A single primary-position photograph cannot show a
movement, so however good it is it can be "representative" at best, and you must
say in "missing" that the gaze positions are not shown. A montage of the nine
(or five) positions is what such a station calls for.

DO NOT reject on the patient's age, sex or ethnicity. The candidate is being
asked to describe a clinical sign, not to guess demographics, and a sign looks
the same whoever carries it. Congenital conditions in particular are almost
always published as photographs of children even when the station's patient is
an adult; that is not a mismatch.

Judge the image on its own merits, not on the page it came from.

Name the modality as exactly one of: external, slit_lamp, fundus, angiogram,
oct, ultrasound, radiology, visual_field, topography, pathology, other. Report
what the image IS, not what the station wanted - a mismatch is caught after you
answer, and guessing the expected one hides it.

Return ONLY a JSON object:
{
  "tier": "faithful" | "representative" | "reject",
  "modality": "<one of the values above>",
  "confidence": <number 0-1>,
  "shows": "what the image actually shows, one sentence",
  "reason": "why you graded it that way, one sentence",
  "missing": "any station sign the image does NOT show, or null",
  "caption": "a neutral caption for the station, naming only the modality and
              laterality - it must NOT give away the diagnosis"
}"""


def build_search_queries(
    db: Session, client: AIClient, station: OsceStation, wanted: str | None = None
) -> list[str]:
    """Three queries, specific to broad, tried in order until one yields a match.

    `wanted` narrows the search to one question's ancillary test - "OCT of the
    right macula showing intraretinal fluid" - instead of the station's signs
    as a whole. An examiner asking the candidate to read an OCT must be handed
    an OCT, not the external photograph that opens the station.
    """
    signs = wanted or station.findings_elicited or station.findings or ""
    fallback = [
        q for q in [
            wanted or " ".join(p for p in [station.diagnosis, "clinical photograph"] if p),
            station.diagnosis or "",
            station.subspecialty or "ophthalmology clinical photograph",
        ] if q
    ]
    try:
        data = client.complete_json(
            task="utility",
            system=QUERY_SYSTEM,
            user=(
                f"SUBSPECIALTY: {station.subspecialty or 'unknown'}\n"
                f"CASE: {station.case_summary or 'unknown'}\n"
                f"SIGNS THE CANDIDATE SHOULD SEE: {signs or 'unknown'}\n"
                f"DIAGNOSIS (for your context only): {station.diagnosis or 'unknown'}"
            ),
            max_tokens=200,
            temperature=0.2,
        )
        queries = [
            str(q).strip().strip('"')
            for q in (data.get("queries") if isinstance(data, dict) else data) or []
            if str(q).strip()
        ]
        return _gaze_first(queries or fallback, signs, station)
    except (AIError, ValueError, AttributeError):
        return _gaze_first(fallback, signs, station)


def _gaze_first(queries: list[str], signs: str, station: OsceStation) -> list[str]:
    """Put the montage phrasing in front, for a station about eye movement.

    Station 7 ran through all three of the model's phrasings and attached from
    the broad one, "multiple cranial nerve palsies" - which had dropped the gaze
    wording altogether and returned a face in primary position. The phrase a
    montage is actually filed under is short, fixed and free to write, so it
    does not need a model to guess it.
    """
    if not wants_gaze_positions(signs, station.diagnosis):
        return queries
    subject = station.diagnosis or station.subspecialty or "ocular motility"
    lead = [
        f"nine positions of gaze photograph {subject}",
        f"ocular motility photographs {subject}",
        "nine positions of gaze photograph montage",
    ]
    return lead + [q for q in queries if q not in lead]


DESCRIBE_SYSTEM = """You are the examiner at an ophthalmology OSCE station. No photograph exists of
what the candidate is meant to look at, so you state the findings aloud, as the
patient in front of them would have demonstrated.

You are given THIS station's recorded findings. State only what those findings
say. You must not add, complete, infer or embellish a single sign. If the
findings do not mention a test, do not report its result. Inventing a plausible
examination finding is the worst thing you can do here: the candidate is marked
against the station's rubric, so a sign you made up is a mark they cannot earn
and an answer that will be marked wrong.

Write 1-4 short sentences in the present tense, in the words an examiner would
use at the bedside. Report raw appearances and measurements only.

Do NOT characterise, classify or interpret what you report. Say what is seen,
not what it amounts to. Naming the pattern is the candidate's job and the thing
being marked - so no "congruous", "incongruous", "macular sparing", "consistent
with", "suggestive of", "in keeping with", "typical of", "pathognomonic", and no
naming of the diagnosis, syndrome, causative organism or underlying disease.

Do not mention management, investigations, prognosis or history. Do not say that
an image is missing or refer to a photograph.

If the findings given are too thin to state anything faithfully, return an empty
description rather than filling the gap.

Return ONLY a JSON object: {"description": "..."}"""


def describe_findings(
    client: AIClient, station: OsceStation, wanted: str | None
) -> tuple[str | None, str | None]:
    """State the signs aloud, for what no photograph could be found for.

    The station's own recorded findings are the source of truth, and the rubric
    only says which of them to cover. Given the rubric alone this wrote fluent,
    confident and wrong examination findings - horizontal motility defects for a
    station about elevation, an orthophoric cover test for a station about a
    squint. It had nothing to be faithful to, so it invented.

    Marked with the model answer task, not the utility one. This is text a
    candidate is examined on, not a mechanical rewording.
    """
    rubric_points = (wanted or "").strip()
    truth = (station.findings_elicited or station.findings or "").strip()
    if not truth and not rubric_points:
        return None, None
    try:
        data = client.complete_json(
            task="model_answer",
            system=DESCRIBE_SYSTEM,
            user=(
                f"SUBSPECIALTY: {station.subspecialty or 'unknown'}\n\n"
                f"THIS STATION'S RECORDED FINDINGS - the only facts you may state:\n"
                f"{truth or '(none recorded)'}\n\n"
                f"THE RUBRIC EXPECTS THE CANDIDATE TO DESCRIBE:\n"
                f"{rubric_points or '(not specified)'}\n\n"
                f"State the findings above that the rubric asks about. Say nothing "
                f"the recorded findings do not contain."
            ),
            max_tokens=320,
            temperature=0.0,
        )
    except (AIError, ValueError, AttributeError):
        logger.warning("Could not describe findings for station %s", station.id)
        return None, None

    text = str((data or {}).get("description") or "").strip()
    if not text:
        # The model is told to return nothing rather than fill a gap, so this
        # is a legitimate outcome - but it used to be the one path that logged
        # nothing at all, which made an absent description indistinguishable
        # from a rejected one.
        logger.info(
            "No description for station %s: the model returned none for %r",
            station.id, (rubric_points or truth)[:120],
        )
        return None, None
    leak = leaked_term(text, station)
    if leak:
        logger.warning(
            "Discarded a description of station %s: it gave away %r", station.id, leak
        )
        return None, None
    # Advisory, not a veto. Three runs in a row it discarded a correct
    # description for using ordinary examination words the findings happened
    # not to contain - "larger" and "constricts" for a dilated pupil with
    # light-near dissociation, then "convergence" for how the near response is
    # tested. Each time the answer was to widen a list that will never be
    # complete, while the station went on having nothing to show.
    #
    # It cannot distinguish paraphrase from invention, so it now tells the
    # reviewer what to look at instead of deciding for them. Naming the
    # diagnosis is still a hard reject above: that one is unambiguous.
    problem = grounding_problem(text, station, rubric_points)
    if problem:
        logger.info("Description of station %s wants checking: %s", station.id, problem)
    return text, problem


# Words too common to count as giving anything away on their own.
_DIAGNOSIS_STOPWORDS = frozenset({
    "left", "right", "bilateral", "eye", "eyes", "with", "and", "the", "of", "a", "an",
    "syndrome", "disease", "chronic", "acute", "secondary", "primary", "ocular",
    "presenting", "presents", "patient", "both", "from", "due", "this", "that",
})

# Language that draws the conclusion instead of reporting the sign. A station
# on reading a visual field was told the defect was "congruous" with "macular
# sparing" - never naming the diagnosis, and handing over the whole answer.
_CONCLUSION_RE = re.compile(
    r"\bcongruous\b|\bincongruous\b|\bspar(?:ed|ing)\b|\bconsistent\s+with\b|"
    r"\bsuggestive\s+of\b|\bin\s+keeping\s+with\b|\btypical\s+of\b|"
    r"\bcharacteristic\s+of\b|\bpathognomonic\b|\bdiagnos\w+\b|\bindicativ\w+\b|"
    r"\bcompatible\s+with\b|\bclassic\s+(?:for|of)\b",
    re.IGNORECASE,
)


# Ordinary examination vocabulary: words that carry no clinical claim, so they
# are not evidence that anything was invented.
_GENERIC_WORDS = frozenset("""
about above across applied appears apparent are both cover covered covering
distance during each either examination examined eye eyes fixation from full
glasses greater half have here his however inspection into left less light
limited lower measured measures measuring more movement movements near noted
normal note observed other outward outwards over patient position positions
present primary reduced removed reveals right same seen shows side slight
slightly small some testing tests than that the their there these this
through under upper upward upwards visible when where which while with
within without would degrees prism prisms cover-test uncover uncovered
correction distance-correction bilateral unilateral symmetric asymmetric
mild moderate marked dense partial complete good poor
turned away feel feels felt pulled loose looser sits lies held lifted
globe globes eyelid eyelids lids lid lash lashes cornea conjunctiva sclera
pupil pupils iris lens disc discs fundus macula retina orbit face
poorly well fully partially freely easily readily barely equally briskly
sluggishly incompletely symmetrically evenly clearly visibly obviously
appears appear appeared seems looks looking towards along across between
larger smaller equal unequal bigger reacts react reacting reaction reactive
responds response constricts constrict constricting dilates dilate dilated
brisk sluggish target fixation blink blinks closes closed opens open
greater lesser difference size shape position movement excursion range
""".split())

# Words are matched on a shared opening, not by stripping suffixes. A suffix
# stemmer reduced "elevation" to "elev" and "elevates" to "elevat", so a
# description that used the verb where the findings used the noun was reported
# as having invented the word.
_ROOT = 5


def _words(text: str | None) -> set[str]:
    return set(re.findall(r"[a-z][a-z'\-]{3,}", (text or "").lower()))


def _grounded(word: str, allowed: set[str]) -> bool:
    if word in allowed:
        return True
    if len(word) < _ROOT:
        return False
    root = word[:_ROOT]
    return any(len(a) >= _ROOT and a[:_ROOT] == root for a in allowed)


def grounding_problem(
    text: str, station: OsceStation, wanted: str | None
) -> str | None:
    """Why this description is not a faithful account of the station, or None.

    Instructing the model was not enough. Told the recorded findings were the
    only facts it could state, it described a retracted upper lid and a
    forward-displaced globe for a station whose findings are a cicatricial
    ectropion of the lower lids - a different condition, stated confidently.

    Invention shows up as a clinical term appearing nowhere in the findings,
    because paraphrasing into plainer words reaches for ordinary vocabulary
    instead: "the lower lids are turned outwards" borrows nothing.
    """
    allowed = (
        _words(station.findings_elicited)
        | _words(station.findings)
        | _words(wanted)
        | _words(station.subspecialty)
        | _GENERIC_WORDS
    )
    if not (_words(station.findings_elicited) | _words(station.findings)):
        return "the station has no recorded findings to be faithful to"

    invented = sorted(
        w for w in _words(text) if len(w) >= _ROOT and not _grounded(w, allowed)
    )
    if invented:
        return f"states {', '.join(invented[:4])}, which the findings do not"

    # This check is a floor, not a guarantee. It cannot tell a faithful
    # paraphrase from an invention, because both reach for words the findings
    # do not contain: "the pupil is larger and constricts poorly" is a correct
    # rendering of "a dilated pupil with light-near dissociation", and reads
    # exactly like the invented "the upper lid is retracted". The generic list
    # is what separates them, and it will always be incomplete. What actually
    # protects a candidate is that no description is shown until it is read.
    #
    # Requiring overlap with the findings' own distinctive words was tried and
    # removed. On the stations where it would matter most, the diagnosis IS the
    # physical sign - "bilateral lower lid ectropion" - so a description that
    # may not name the answer has to reach for plain words instead: "both lower
    # lids are turned outwards". That shares no vocabulary with the findings and
    # is exactly what a good description looks like.
    #
    # It also means a description that states the opposite of the findings - a
    # cover test reported as showing no movement on a station about a squint -
    # invents no term and passes. Nothing deterministic catches that, which is
    # why no description reaches a candidate until it has been read.
    return None


def leaked_term(text: str, station: OsceStation) -> str | None:
    """What this description gives away, or None if it only reports signs.

    Deterministic rather than another model call: it has to be reliable, it
    runs on every description, and it has to be free.

    Two ways to give the game away. Naming the condition is the obvious one,
    and is checked against the station's diagnosis and its case summary, since
    the summary names it too. The subtler one is characterising the sign -
    "congruous, with macular sparing" names no diagnosis at all and is still
    the answer to the question being asked.
    """
    conclusion = _CONCLUSION_RE.search(text)
    if conclusion:
        return conclusion.group(0)

    # Only the diagnosis, not the case summary. The summary is prose full of
    # ordinary clinical vocabulary - checking it rejected "there is a defect in
    # the left half of each field" because the summary happened to say "field".
    # Anatomy is not a giveaway. "Adie's pupil" made "pupil" a forbidden word,
    # and a dilated pupil with light-near dissociation cannot be described
    # without it - so the only possible description was discarded every time.
    # The same trap sits under every diagnosis named after a structure: optic
    # disc drusen, band keratopathy, macular hole. What gives the answer away
    # is the distinctive part, "Adie".
    innocuous = _DIAGNOSIS_STOPWORDS | _GENERIC_WORDS
    lowered = text.lower()
    for word in re.findall(r"[a-z][a-z'\-]{3,}", (station.diagnosis or "").lower()):
        if word not in innocuous and word in lowered:
            return word
    return None


def verify_image(
    db: Session,
    client: AIClient,
    station: OsceStation,
    data: bytes,
    media_type: str,
    wanted: str | None = None,
) -> dict[str, Any]:
    """Ask a vision model whether this image really shows the station's signs.

    `wanted` swaps the station's signs for one question's requirement, so the
    same verification bar - right modality, no annotation, right pathology -
    is applied to exactly what that question asks the candidate to read.
    """
    signs = wanted or station.findings_elicited or station.findings or "(not recorded)"
    content = [
        TextPart(
            f"SUBSPECIALTY: {station.subspecialty or 'unknown'}\n"
            f"CASE: {station.case_summary or 'unknown'}\n\n"
            f"CLINICAL SIGNS THE CANDIDATE IS EXPECTED TO SEE:\n{signs}\n\n"
            f"Judge the image below."
        ),
        ImagePart(data=data, media_type=media_type),
    ]
    data_out = client.complete_json(task="vision", system=VERIFY_SYSTEM, user=content)
    if not isinstance(data_out, dict):
        raise ValueError("Image verification did not return a JSON object")
    return data_out


def expected_modalities_for(station: OsceStation, wanted: str | None) -> frozenset[str]:
    """What kind of image this figure has to be.

    A figure requested by a question states its own requirement. The station's
    opening image is governed instead by the first thing the candidate is asked
    to do: "examine the anterior segment" cannot be answered by an angiogram,
    however well that angiogram matches the station's findings overall.
    """
    if wanted:
        return expected_modalities(wanted)
    for prompt in station.prompts or []:
        text = str(prompt.get("text") or "").strip()
        if text:
            return expected_modalities(text)
    tasks = station.tasks or []
    if tasks:
        first = tasks[0]
        return expected_modalities(first if isinstance(first, str) else str(first.get("text") or ""))
    return frozenset()


def source_image_for_station(
    db: Session,
    client: AIClient,
    station: OsceStation,
    job_id: int | None = None,
    figure: OsceFigure | None = None,
) -> dict[str, Any]:
    """Find, verify and attach one image. Returns an outcome summary.

    Pass `figure` to fill a particular one - an ancillary test a question asks
    the candidate to read. Its `wanted_description` then drives both the search
    and the verification. Left out, this fills the station's own image.
    """
    store = SettingsStore(db)
    provider = build_provider(store)
    if provider is None:
        raise ImageSearchError("Image search is disabled (provider is 'none').")

    wanted: str | None = None
    if figure is not None:
        wanted = figure.wanted_description
    else:
        # The rubric decides what the opening image has to show, not the case
        # findings. Searching the findings produced a good picture of the
        # disease and a station whose marks could not be earned from it; the
        # first view states the signs the candidate is actually marked on.
        views = station_views(station)
        opening = views[0].wanted_description if views else (
            station.findings_elicited or station.findings
        )

        # The station's own figure, never a question's. Taking the lowest
        # position outright overwrote question C's MRI on station 158 with the
        # gaze montage this run had just found: the montage ended up attached
        # to "what does this scan show?", and the motility task it was searched
        # for still opened on nothing. A figure a question owns is that
        # question's, and is filled by its own request.
        figure = next((f for f in opening_figures(station)), None)
        if figure is None:
            figure = OsceFigure(
                station_id=station.id,
                position=max((f.position for f in station.figures), default=-1) + 1,
                wanted_description=opening,
            )
            db.add(figure)
            db.flush()
        elif views and figure.wanted_description != opening:
            # Re-sourcing an existing figure has to re-read the rubric too.
            # Setting this only on creation meant every station sourced before
            # the change kept searching its case findings for ever - including
            # the one this was first tested on.
            figure.wanted_description = opening
            db.flush()

    # Whatever this run finds replaces what the last one said, so the previous
    # attempt's wording must not survive it - and approval must not survive the
    # wording. Leaving it in place kept two wrong descriptions on the station
    # through a re-run that was meant to replace them.
    figure.described_findings = None
    figure.described_findings_approved = False

    expected = expected_modalities_for(station, wanted)
    queries = build_search_queries(db, client, station, wanted)
    per_query = store.get_int("imagesearch.results_per_query", 6)
    rejections: list[str] = []
    # confidence, candidate, downloaded, verdict, and the query that found it
    best_representative: tuple[float, Any, Any, dict[str, Any], str] | None = None

    # Never offer back an image the user has already turned down.
    already_rejected = set(figure.rejected_urls or [])
    auto_approve = store.get_bool("imagesearch.auto_approve", True)

    # Work specific -> broad, stopping at the first faithful match. A
    # representative hit found early is held in reserve rather than accepted
    # immediately, in case a later query turns up something faithful.
    for query in queries:
        figure.search_query = query
        try:
            check_and_consume_quota(db, store)
            candidates = provider.search(query, per_query)
        except ImageQueryError as exc:
            # This phrase, not this account. Brave answered HTTP 500 to "nine
            # positions of gaze photograph Congenital fibrosis of extraocular
            # muscles." and both batches then running were failed outright,
            # leaving 35 stations unsourced over one bad query.
            logger.info("Search failed for %r: %s", query, exc)
            rejections.append(f"search failed for '{query}': {exc}")
            continue
        except ImageSearchError:
            raise
        if not candidates:
            rejections.append(f"no results for '{query}'")
            continue

        for candidate in candidates:
            if candidate.image_url in already_rejected:
                continue
            downloaded = download_candidate(candidate)
            if downloaded is None:
                # Silently skipping these left "tried 3 queries" and no reason
                # at all in the notes, when in fact every result was a
                # thumbnail too small to describe.
                rejections.append(
                    f"{candidate.source or 'source'}: not usable (too small, wrong "
                    "format, or would not download)"
                )
                continue
            blob, content_type, _width, _height = downloaded

            try:
                verdict = verify_image(db, client, station, blob, content_type, wanted)
            except Exception as exc:  # noqa: BLE001 - try the next candidate
                logger.debug("Verification error on a candidate: %s", exc)
                continue

            tier = str(verdict.get("tier") or "reject").lower()
            confidence = as_float(verdict.get("confidence"), 0.0)

            # The grader judges pathology; this judges whether the image is the
            # examination that was asked for. An angiogram passed on the
            # station's disc findings is still the wrong thing to hand someone
            # told to examine the anterior segment.
            mismatch = modality_mismatch(expected, verdict.get("modality"))
            if mismatch:
                rejections.append(f"{candidate.source or 'source'}: {mismatch}")
                continue

            if tier == "faithful" and confidence >= MIN_MATCH_CONFIDENCE:
                return _attach(db, client, station, figure, candidate, downloaded, verdict,
                               "faithful", confidence, query, len(rejections), auto_approve)

            if tier == "representative" and confidence >= MIN_REPRESENTATIVE_CONFIDENCE:
                if best_representative is None or confidence > best_representative[0]:
                    best_representative = (confidence, candidate, downloaded, verdict, query)
                continue

            rejections.append(
                f"{candidate.source or 'source'}: {verdict.get('reason') or 'rejected'}"
            )

    if best_representative is not None:
        confidence, candidate, downloaded, verdict, found_by = best_representative
        # The query that found it, not whichever was tried last. Station 7's
        # montage was recorded against "multiple cranial nerve palsies", the
        # broad phrase that had already failed, which reads as though the gaze
        # wording had made no difference when it was the whole reason.
        return _attach(db, client, station, figure, candidate, downloaded, verdict,
                       "representative", confidence, found_by,
                       len(rejections), auto_approve)

    figure.image_id = None
    figure.verification_status = "rejected"
    figure.verification_notes = (
        f"Tried {len(queries)} quer(ies): {'; '.join(queries)}. "
        + " | ".join(rejections[:5])
    )[:4000]

    # Last resort, and only here: every query has been tried and not one
    # candidate survived, not even a representative. Rather than leave a
    # station whose marks cannot be earned, the examiner states the findings
    # the way a real patient would demonstrate them.
    described, concern = describe_findings(
        client, station, wanted or figure.wanted_description
    )
    if described:
        figure.described_findings = described
        figure.verification_status = "described"
        if concern:
            figure.verification_notes = (
                f"{figure.verification_notes or ''}  "
                f"[Check the stated findings: {concern}]"
            )[:4000]
    db.commit()
    return {"attached": False, "queries": queries, "reason": "all candidates rejected",
            "rejected": len(rejections)}


def _attach(
    db: Session,
    client: AIClient,
    station: OsceStation,
    figure: OsceFigure,
    candidate: Any,
    downloaded: Any,
    verdict: dict[str, Any],
    tier: str,
    confidence: float,
    query: str,
    rejected: int,
    auto_approve: bool = True,
) -> dict[str, Any]:
    # OsceFigure exposes the same `image_id` the writer sets, so the
    # deduplicate-and-store path is reused as-is.
    image = attach_image_to_figure(db, figure, candidate, downloaded)
    figure.image_id = image.id
    figure.caption = str(verdict.get("caption") or "").strip() or None
    figure.verification_status = tier
    notes = str(verdict.get("shows") or "").strip()
    missing = str(verdict.get("missing") or "").strip()
    if tier == "representative" and missing and missing.lower() not in {"null", "none"}:
        notes = f"{notes}  [Does NOT show: {missing}]"
        # The image is a genuine picture of the pathology but cannot answer
        # every expectation, and those are marks the candidate is about to be
        # asked for. State the ones it misses, the way the patient would have
        # demonstrated them, so the rubric stays earnable alongside the image.
        described, concern = describe_findings(client, station, missing)
        figure.described_findings = described
        if described and concern:
            notes = f"{notes}  [Check the stated findings: {concern}]"
    else:
        figure.described_findings = None
    figure.verification_notes = notes or None
    figure.match_confidence = confidence
    figure.search_query = query
    # Vision verification has already discarded diagrams, wrong modalities and
    # unrelated pathology, so a faithful image is shown straight away and the
    # user rejects the ones that are wrong. Holding everything back for
    # approval means stations start with no image at all, which is worse.
    #
    # A representative one is different, and used to be auto-approved too. The
    # model has just written down which of the station's signs it does NOT
    # show, and those are the marks the candidate is about to be asked for. It
    # is held for review rather than published as though it could answer the
    # question - the whole complaint about stations whose images cannot be
    # described to earn the marks.
    figure.is_approved = auto_approve and tier == "faithful"
    db.commit()
    return {
        "attached": True, "tier": tier, "query": query,
        "confidence": confidence, "source_url": candidate.image_url,
        "rejected": rejected,
    }


JOB_VERIFY_STATION_FIGURES = "verify_station_figures"

# What ingest wrote before its figures were checked. Anything else has been
# through a vision model and must not be re-graded for free.
UNCHECKED_STATUSES = frozenset({"verified", "unverified", "", None})


def verify_ingested_figures(
    db: Session, client: AIClient, station: OsceStation
) -> dict[str, Any]:
    """Check the photographs lifted out of the examiners' report.

    Ingest attached every image on a block's pages, in page order, marked
    verified and approved, on the reasoning that a photograph printed in the
    report is by definition the real one. Two things are wrong with that.

    The first is that a block spans pages, so "every image on them" is not the
    station's photographs - it is those plus mark-distribution charts, tables
    set as pictures, and whatever the neighbouring station used. One station
    from the 2023 Semester 2 paper collected fifteen figures this way.

    The second is that being genuine is not the same as answering the question.
    A fundus photograph is no use to "please examine the anterior segment": the
    candidate is asked for signs the image cannot show, and every mark for them
    is unearnable - the exact failure the sourcing path was built to prevent,
    reappearing through a door it does not watch.

    So they go through the same gate: the same vision grader, the same modality
    check against what the first task asks the candidate to do. What survives
    keeps its place. What does not is unapproved rather than deleted, because
    the image did come out of the real paper and an administrator may want to
    put it back against a different question.
    """
    expected = expected_modalities_for(station, None)
    signs = station.findings_elicited or station.findings or ""
    kept, rejected, skipped = 0, 0, 0

    for figure in sorted(station.figures, key=lambda f: f.position):
        if figure.image_id is None or figure.verification_status not in UNCHECKED_STATUSES:
            skipped += 1
            continue
        image = db.get(Image, figure.image_id)
        if image is None or image.origin != "pdf":
            skipped += 1
            continue

        try:
            verdict = verify_image(db, client, station, image.data, image.content_type)
        except Exception as exc:  # noqa: BLE001 - one figure must not stop the rest
            logger.warning("Could not verify figure %s: %s", figure.id, exc)
            skipped += 1
            continue

        tier = str(verdict.get("tier") or "reject").lower()
        confidence = as_float(verdict.get("confidence"), 0.0)
        mismatch = modality_mismatch(expected, verdict.get("modality"))
        figure.match_confidence = confidence
        figure.verification_notes = str(verdict.get("shows") or "").strip() or None
        figure.caption = (
            figure.caption or str(verdict.get("caption") or "").strip() or None
        )

        if mismatch or tier == "reject" or confidence < MIN_REPRESENTATIVE_CONFIDENCE:
            figure.verification_status = "rejected"
            figure.is_approved = False
            figure.verification_notes = (
                f"{figure.verification_notes or ''} "
                f"[Not shown: {mismatch or verdict.get('reason') or 'rejected'}]"
            ).strip()[:4000]
            rejected += 1
            continue

        figure.verification_status = tier
        # A lower bar than a web image, deliberately. This one came out of the
        # examiners' report: it IS the photograph the real candidates were
        # shown, so "representative" here means the grader could not tie every
        # recorded sign to it, not that some stranger's eye was substituted for
        # the patient's. Holding those back would throw away the best images in
        # the bank. Only a chart, a diagram or the wrong examination is dropped.
        figure.is_approved = True
        kept += 1

    db.commit()
    return {"kept": kept, "rejected": rejected, "skipped": skipped}


def bind_ingested_figures_to_questions(
    db: Session, client: AIClient, station: OsceStation
) -> dict[str, Any]:
    """Give a question the report's own investigation, rather than buying one.

    Grading every ingested figure against the station's opening task was too
    blunt. "Please examine the patient's eye movements" expects an external
    photograph, so the four MRIs printed with that station were all marked the
    wrong modality and dropped - while question C of the same station asks the
    candidate to read an MRI of the brain, and the sourcing run was about to
    search the web for one.

    A figure nothing has claimed is therefore offered to each unanswered
    question whose investigation it names. The match must be exact: a request
    naming no modality of its own is skipped rather than guessed at, and a
    Pentacam is not accepted for a question that asked for a UBM. The vision
    grader then judges it against that question's own wording, so binding never
    rests on the caption alone.
    """
    prompts = list(station.prompts or [])
    claimed = {i for p in prompts for i in _bound_ids(p)}
    spare = [
        f for f in sorted(station.figures, key=lambda f: f.position)
        if f.image_id and f.id not in claimed
    ]
    if not spare:
        return {"bound": 0}

    bound = 0
    for prompt in prompts:
        wanted = str(prompt.get("image_wanted") or "").strip()
        if not wanted or _bound_ids(prompt) or prompt.get("image_impossible"):
            continue
        asked = named_modality(wanted)
        if asked is None:
            continue

        for figure in list(spare):
            image = db.get(Image, figure.image_id)
            if image is None or image.origin != "pdf":
                continue
            if named_modality(f"{figure.caption or ''} {figure.verification_notes or ''}") != asked:
                continue
            try:
                verdict = verify_image(
                    db, client, station, image.data, image.content_type, wanted
                )
            except Exception as exc:  # noqa: BLE001 - try the next figure
                logger.debug("Could not verify figure %s for a question: %s", figure.id, exc)
                continue
            tier = str(verdict.get("tier") or "reject").lower()
            if tier == "reject" or modality_mismatch(
                expected_modalities(wanted), verdict.get("modality")
            ):
                continue

            figure.wanted_description = wanted
            figure.verification_status = tier
            figure.match_confidence = as_float(verdict.get("confidence"), 0.0)
            figure.caption = str(verdict.get("caption") or "").strip() or figure.caption
            figure.verification_notes = str(verdict.get("shows") or "").strip() or None
            figure.is_approved = True
            prompt["figure_id"] = figure.id
            spare.remove(figure)
            bound += 1
            break

    if bound:
        station.prompts = [dict(p) for p in prompts]
        flag_modified(station, "prompts")
        db.commit()
    return {"bound": bound}


def _bound_ids(prompt: dict[str, Any]) -> list[int]:
    ids = [i for i in (prompt.get("figure_ids") or []) if i]
    first = prompt.get("figure_id")
    if first and first not in ids:
        ids.insert(0, first)
    return ids


@register_handler(JOB_VERIFY_STATION_FIGURES)
def handle_verify_station_figures(ctx: JobContext) -> bool:
    """One station per chunk: grade the figures ingest took on trust."""
    station_ids: list[int] = ctx.payload.get("station_ids") or []
    if not station_ids:
        raise JobHandlerError("No station_ids supplied")
    if not ctx.job.total_steps:
        ctx.set_total(len(station_ids))

    index = ctx.cursor_get("index", 0)
    if index >= len(station_ids):
        return True

    station = ctx.db.get(OsceStation, station_ids[index])
    if station is not None:
        try:
            client = AIClient(ctx.db)
            outcome = verify_ingested_figures(ctx.db, client, station)
            # A figure the opening task had no use for may be exactly what a
            # later question asks the candidate to read.
            outcome.update(bind_ingested_figures_to_questions(ctx.db, client, station))
            tally = dict((ctx.job.result or {}).get("figures", {}))
            for key in ("kept", "rejected", "skipped", "bound"):
                tally[key] = tally.get(key, 0) + outcome.get(key, 0)
            ctx.set_result(figures=tally)
        except Exception as exc:  # noqa: BLE001
            ctx.db.rollback()
            logger.exception("Could not verify the figures of station %s", station.id)
            log_error(ctx.db, source="osce_images", message=str(exc),
                      context={"station_id": station.id})

    ctx.cursor_set(index=index + 1)
    ctx.advance(1, f"Figures: {index + 1} of {len(station_ids)}")
    return index + 1 >= len(station_ids)


def stations_with_unchecked_figures(db: Session) -> list[int]:
    """Stations still showing images no vision model has ever looked at."""
    ids = db.execute(
        select(OsceFigure.station_id)
        .join(Image, Image.id == OsceFigure.image_id)
        .where(Image.origin == "pdf")
        .where(
            OsceFigure.verification_status.is_(None)
            | OsceFigure.verification_status.in_(["verified", "unverified", ""])
        )
        .distinct()
    ).scalars().all()
    return sorted(set(ids))


def source_coverage_images(
    db: Session, client: AIClient, station: OsceStation, job_id: int | None = None
) -> dict[str, Any]:
    """Fill every view the station's rubric needs, not just the opening one.

    A task marking signs in both eyes cannot be answered from one photograph:
    the marks for the other eye are unearnable however good the candidate is.
    Each view the rubric implies gets its own figure, so the whole rubric is
    describable.
    """
    views = station_views(station)
    if len(views) <= 1:
        return {"attached": 0, "failed": 0, "views": len(views)}

    # The opening figure already covers the first view; it was sourced from the
    # first task and is what the candidate sees on entering.
    #
    # Indexed by what each figure wants, not just the ones that found an image.
    # Keying on `image_id is not None` meant a view that had come back with
    # nothing - or with a written description instead - was treated as absent,
    # so every re-run added another figure for it and left the old one behind
    # with its stale text still attached to the station.
    by_wanted: dict[str, OsceFigure] = {}
    for existing in station.figures:
        key = (existing.wanted_description or "").strip().lower()
        if key and key not in by_wanted:
            by_wanted[key] = existing
    attached, failed = 0, 0

    for view in views[1:]:
        wanted = view.wanted_description
        figure = by_wanted.get(wanted.strip().lower())
        if figure is not None and figure.image_id is not None:
            continue  # already answered by a real image
        if figure is None:
            figure = OsceFigure(
                station_id=station.id,
                position=len(station.figures) + 1,
                wanted_description=wanted,
            )
            db.add(figure)
        db.flush()
        try:
            outcome = source_image_for_station(db, client, station, job_id, figure=figure)
        except ImageSearchError:
            raise
        except Exception as exc:  # noqa: BLE001 - one view must not stop the rest
            db.rollback()
            logger.exception("Could not source the %s view for station %s",
                             view.laterality, station.id)
            log_error(db, source="osce_images", message=str(exc),
                      context={"station_id": station.id, "view": view.laterality})
            failed += 1
            continue
        if outcome.get("attached"):
            attached += 1
        else:
            failed += 1

    db.commit()
    return {"attached": attached, "failed": failed, "views": len(views)}


def source_prompt_images(
    db: Session, client: AIClient, station: OsceStation, job_id: int | None = None
) -> dict[str, Any]:
    """Find the ancillary images the station's questions ask the candidate to read.

    A question that says "This is an MRI of the orbits, what does it show?" is
    only askable if there is an MRI. The question states what it needs, this
    goes and gets it, and the figure is bound to that question so it appears
    when the question does - showing it from the start would hand the candidate
    the diagnosis before they have described anything.

    A question may ask for more than one: "OCT of the right macula showing CNVM
    and fluorescein angiogram of both eyes showing multifocal choroiditis" is
    two investigations, and no one image is both. Searching the whole string
    returned nothing at all, so nine such questions kept asking for something
    that was never going to arrive. Each investigation gets its own figure and
    the question carries them all.
    """
    prompts = list(station.prompts or [])
    attached, failed, skipped = 0, 0, 0

    for index, prompt in enumerate(prompts):
        wanted = prompt.get("image_wanted")
        if not wanted or prompt.get("figure_id"):
            continue

        # No search will ever fill a serology titre or a textbook diagram. Left
        # in, they were paid for on every run and reported as merely missing.
        impossible = unsourceable_reason(wanted)
        if impossible:
            prompt["image_impossible"] = impossible
            skipped += 1
            continue

        bound: list[int] = []
        for part in split_investigations(wanted):
            figure = OsceFigure(
                station_id=station.id,
                # After every figure the station already has: position 0 is the
                # patient, and ordering keeps the opening image first.
                position=len(station.figures) + len(bound) + 1,
                wanted_description=part,
            )
            db.add(figure)
            db.flush()

            try:
                outcome = source_image_for_station(db, client, station, job_id, figure=figure)
            except ImageSearchError:
                raise
            except Exception as exc:  # noqa: BLE001 - one question must not stop the rest
                db.rollback()
                logger.exception("Could not source the image for prompt %s", prompt.get("label"))
                log_error(db, source="osce_images", message=str(exc),
                          context={"station_id": station.id, "prompt": prompt.get("label")})
                failed += 1
                continue

            if outcome.get("attached"):
                bound.append(figure.id)
                attached += 1
            else:
                # Nothing suitable was found. The question would ask the
                # candidate to read a blank screen, so the figure is left for an
                # admin to fill by hand and the request stays on the question.
                failed += 1

        if bound:
            # Bound by id, so the sitting shows these at this question alone.
            # `figure_id` stays the first of them: it is what every reader of a
            # station still expects, and a question with one investigation - the
            # common case - is unchanged by any of this.
            prompt["figure_id"] = bound[0]
            prompt["figure_ids"] = bound

    if attached or skipped:
        # Rebinding alone does not survive: the intermediate commit inside
        # image attachment leaves the column looking unchanged, and the
        # figure_id is quietly dropped. Flagging it dirty is what persists it.
        station.prompts = [dict(p) for p in prompts]
        flag_modified(station, "prompts")
    db.commit()
    return {"attached": attached, "failed": failed, "impossible": skipped}


@register_handler(JOB_SOURCE_STATION_IMAGES)
def handle_source_station_images(ctx: JobContext) -> bool:
    """One station per chunk: search, verify, attach."""
    station_ids: list[int] = ctx.payload.get("station_ids") or []
    if not station_ids:
        raise JobHandlerError("No station_ids supplied")

    if not ctx.job.total_steps:
        ctx.set_total(len(station_ids))

    index = ctx.cursor_get("index", 0)
    if index >= len(station_ids):
        return True

    station = ctx.db.get(OsceStation, station_ids[index])
    if station is not None:
        try:
            client = AIClient(ctx.db)
            # Sourcing the whole bank is dozens of stations that only want an
            # ancillary image; `only_missing` keeps the spend on those.
            if ctx.payload.get("only_missing") and opening_image_is_settled(station):
                kept = list((ctx.job.result or {}).get("kept", []))
                kept.append(station.id)
                ctx.set_result(kept=kept)
            else:
                outcome = source_image_for_station(ctx.db, client, station, job_id=ctx.job.id)
                key = "attached" if outcome.get("attached") else "no_image"
                done = list((ctx.job.result or {}).get(key, []))
                done.append(station.id)
                ctx.set_result(**{key: done})

            # One photograph cannot carry a rubric that marks both eyes, so
            # the remaining views are filled before the station is called done.
            coverage = source_coverage_images(ctx.db, client, station, job_id=ctx.job.id)
            if coverage["attached"] or coverage["failed"]:
                tally = dict((ctx.job.result or {}).get("coverage", {}))
                tally["attached"] = tally.get("attached", 0) + coverage["attached"]
                tally["failed"] = tally.get("failed", 0) + coverage["failed"]
                ctx.set_result(coverage=tally)

            # The questions may each need an image of their own on top of the
            # one the candidate opens on.
            for_prompts = source_prompt_images(ctx.db, client, station, job_id=ctx.job.id)
            if any(for_prompts.values()):
                tally = dict((ctx.job.result or {}).get("prompt_images", {}))
                for key in ("attached", "failed", "impossible"):
                    tally[key] = tally.get(key, 0) + for_prompts.get(key, 0)
                ctx.set_result(prompt_images=tally)
        except ImageSearchError as exc:
            # Quota or credentials: every remaining station would fail too.
            ctx.db.rollback()
            log_error(ctx.db, source="osce_images", message=str(exc),
                      context={"station_id": station.id})
            raise JobHandlerError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            ctx.db.rollback()
            logger.exception("Image sourcing failed for station %s", station.id)
            log_error(ctx.db, source="osce_images", message=str(exc),
                      context={"station_id": station.id})
            failed = list((ctx.job.result or {}).get("failed", []))
            failed.append(station.id)
            ctx.set_result(failed=failed)

    ctx.cursor_set(index=index + 1)
    ctx.advance(1, f"Images: {index + 1} of {len(station_ids)}")
    return index + 1 >= len(station_ids)


# More than one picture of the eyes, as the vision model reports it. Both of
# these must match: a "multi-panel photograph, without and with glasses" is a
# montage of nothing to do with movement, and one photograph "in primary gaze"
# is the single position the whole check exists to catch.
_MONTAGE_RE = re.compile(
    r"\bmontage\b|\bmulti[- ]panel\b|\bnine[- ](?:panel|gaze)\b|\bpanels\b|"
    r"\b(?:series|serial|several|multiple|composite)\b.{0,40}\bphotograph",
    re.IGNORECASE,
)
_GAZE_POSITION_RE = re.compile(
    r"\bgaze\b|\bpositions?\s+of\s+gaze\b|\bup[- ]?gaze\b|\bdown[- ]?gaze\b|"
    r"\bpositions?\s+of\s+(?:the\s+)?eyes?\b|\bductions?\b|\bversions?\b",
    re.IGNORECASE,
)


def wants_gaze_montage(station: OsceStation, figure: OsceFigure) -> bool:
    """Whether this figure was sourced without knowing it needed gaze positions.

    A motility station whose opening photograph is a single primary-position
    face shot looks perfectly good to every other test here - right modality,
    right pathology, high confidence - and still cannot be described. The
    deficit only exists across the positions of gaze.

    What answers it is the description the vision model wrote when the image was
    attached, which is free to read and is the only stored record of how many
    panels the image has. Of the fourteen stations first flagged, five already
    had a nine-panel series the searcher had stumbled into, and re-sourcing those
    would have paid to replace a perfect image.

    Deliberately NOT read from `wanted_description`. Asking for the montage was
    the first test written, and it answered the wrong question: after a
    re-source the phrase is there whether or not a montage was found, so five
    stations still showing one position went quiet the moment they had been
    searched. What matters is the image, not the request.

    An image with nothing recorded about it counts as needing one. That is the
    figure sourced before any of this, which is exactly the case to look at.
    """
    views = station_views(station)
    if not views or not views[0].gaze:
        return False
    described = f"{figure.verification_notes or ''} {figure.caption or ''}"
    return not (_MONTAGE_RE.search(described) and _GAZE_POSITION_RE.search(described))


def opening_figures(station: OsceStation) -> list[OsceFigure]:
    """The figures the candidate meets on entering, in order.

    A figure bound to a question is not one of them: it appears when that
    question does. Counting them together is how a motility station whose
    question C owns an MRI came to look as though it had an opening image, so
    no gaze montage was ever searched for and the candidate was asked to
    examine eye movements with nothing but a brain scan on screen.
    """
    claimed = {i for p in (station.prompts or []) for i in _bound_ids(p)}
    return sorted(
        (f for f in station.figures if f.id not in claimed),
        key=lambda f: f.position,
    )


def opening_image_is_settled(station: OsceStation) -> bool:
    """Whether the station's own image is good enough to leave alone.

    Every other image a station needs is missing or it is not, and the cost of
    finding it is unavoidable. The opening one is different: most stations in a
    batch already have a perfectly good photograph and are in the batch only
    because a question further down wants an MRI. Re-sourcing those spends a
    search and a vision call per station to replace a good image with another
    good image - and drops the description an administrator already approved.

    The test is deliberately the one `scripts/audit_station_images.py` applies to
    a figure. The two disagreeing would mean a batch either paid for stations the
    audit calls fine, or skipped ones it will flag again afterwards.
    """
    figure = next((f for f in opening_figures(station) if f.image_id), None)
    if figure is None or figure.image_id is None or not figure.is_approved:
        return False
    if wants_gaze_montage(station, figure):
        return False
    # A representative image is a picture of the right disease and the wrong
    # patient; it is worth another search. `faithful` is what the vision model
    # writes now, `verified` what it wrote before the tiers were named.
    if figure.verification_status not in {"faithful", "verified"}:
        return False
    # No confidence at all is a figure from before the score was recorded, not a
    # bad one - it is left alone, as the audit leaves it.
    return (figure.match_confidence or 1.0) >= SETTLED_MATCH_CONFIDENCE


def stations_needing_images(db: Session) -> list[int]:
    """Stations with no verified image yet, or a question still waiting for one."""
    with_image = set(
        db.execute(
            select(OsceFigure.station_id).where(OsceFigure.image_id.is_not(None))
        ).scalars().all()
    )
    all_ids = db.execute(select(OsceStation.id).order_by(OsceStation.id)).scalars().all()
    needed = [i for i in all_ids if i not in with_image]

    # A station can have its opening photograph and still be unusable, because
    # a question asks the candidate to read an MRI that was never sourced, or
    # because its rubric marks both eyes and only one was ever photographed.
    for station in db.execute(select(OsceStation).order_by(OsceStation.id)).scalars():
        if station.id in needed:
            continue
        if any(
            p.get("image_wanted") and not p.get("figure_id")
            for p in (station.prompts or [])
        ):
            needed.append(station.id)
            continue
        # Only the views the candidate opens on. A question's own investigation
        # is counted by the check above, and counting it twice made a station
        # whose question owns a scan look covered for its examination task.
        if len([f for f in opening_figures(station) if f.image_id]) < len(
            station_views(station)
        ):
            needed.append(station.id)
    return sorted(needed)

