"""Make each question honest about the image that actually arrived.

A question is written before anyone knows whether its image can be found.
`_unshowable_questions` checks at that moment that the question asked for one,
which is the earliest it can be checked and, as it turns out, too early: it
tests intent. Whether an image really arrived is only knowable after sourcing
has run, and until now nothing looked again.

The result reached candidates. On 7 Aug 2026 a nine-station circuit included
"This is his examination and ocular biometry data. Talk me through what they
show" with nothing on screen, and "Talk me through these retinal images" -
plural, both eyes, with autofluorescence - showing a single photograph of one
eye. Marks were apportioned to findings that were never displayed, so the score
understated the candidate rather than measuring them.

Across the bank that was 33 of 107 investigation questions.

The remedy is never to discard an image or to raise a threshold. Holding images
back for approval once left stations showing nothing at all, and that is a
worse failure than a loose match. Here the *question* moves instead:

  - Fewer images than it asked for -> name only the ones that came.
  - None at all -> the examiner states the result, as an examiner does when
    the candidate cannot be handed a printout; and where the station's own
    record does not say what the investigation showed, the question becomes
    what the candidate would expect to see, which is answerable and invents
    nothing.

Both keep the question and its marks. Neither touches a figure.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.models import OsceFigure, OsceStation
from app.services.ai import AIClient
from app.services.errors import log_error
from app.services.jobs.runner import JobContext, JobHandlerError, register_handler
from app.services.osce.prompts import PRESENTS_INVESTIGATION_RE

logger = logging.getLogger(__name__)

JOB_RECONCILE_QUESTIONS = "reconcile_station_questions"

TRIM = "trim"
STATE = "state"
UNCHANGED = "unchanged"

SYSTEM_PROMPT = """\
You are a RANZCO examiner correcting one question of an OSCE station so that it \
matches what the candidate can actually see on the screen.

You will be given the question as it stands, what it asked to be shown, and \
what is really there. Rewrite ONLY the sentence or clause that refers to the \
images. Everything else - what is being asked, the clinical content, the \
number of differentials requested - must survive unchanged.

MODE "trim": some of the requested images arrived and some did not. Name only \
the ones that are really there. "These are his FAF, disc OCT and visual fields" \
with only the first two present becomes "These are his FAF and disc OCT". Do \
not mention what is missing.

MODE "state": nothing is on screen. An examiner who cannot hand over a \
printout says what it showed and asks the same question about it. Use ONLY \
findings recorded for this station. "This is his automated visual field. Talk \
me through what it shows" becomes "His automated visual field shows a dense \
superior arcuate defect respecting the horizontal midline. What does that tell \
you?"

If the station's record does not say what that investigation showed, DO NOT \
invent a result. Turn the question into what the candidate would expect \
instead: "This is her Quantiferon Gold result. What does it show?" becomes \
"What would you expect her Quantiferon Gold result to show, and how would it \
change your management?" Report this as "expected" so it can be counted.

Never state a result the record does not support. A question the candidate can \
reason about is worth more than a confident invention, and an invented result \
that contradicts the marking key costs them marks for being right.

Return JSON only:
{"text": "<the rewritten question>", "basis": "trim" | "recorded" | "expected"}"""


def _shown_figures(db: Session, station: OsceStation) -> dict[int, str]:
    """Figures the candidate will really see, mapped to the words describing
    them. Attached and approved - a figure the vision gate held back is bound
    to its question and invisible, so it must not count as shown."""
    rows = db.execute(
        select(OsceFigure.id, OsceFigure.caption, OsceFigure.wanted_description).where(
            OsceFigure.station_id == station.id,
            OsceFigure.image_id.is_not(None),
            OsceFigure.is_approved.is_(True),
        )
    ).all()
    # The caption alone, because it describes what the image IS - written after
    # a vision model looked at it. `wanted_description` is what was asked for,
    # and folding it in makes the comparison answer itself: a figure requested
    # as "fundus photographs and autofluorescence" would appear to show both
    # however little arrived, and the question asking for both would look
    # served. That is station 194, and it is the whole failure this exists to
    # catch. It is used only when there is no caption at all, where a stale
    # description still beats knowing nothing.
    return {
        fid: (caption or wanted or "") for fid, caption, wanted in rows
    }


# Fine-grained on purpose. `split_investigations` answers "what should I go and
# buy", and `expected_modalities` answers "is this the right kind of picture" -
# both are deliberately coarse, and reconciliation inherited that coarseness to
# its cost. "Bilateral wide-field colour fundus photographs and fundus
# autofluorescence" is one request to the splitter and one modality to the
# gate, so a question asking for both and showing one photograph read as fully
# served. That was the station a real circuit scored 17.5% on.
#
# Here the question is different again - "does the wording match what is on the
# screen" - so CT is not MRI, a fundus photograph is not autofluorescence, and
# topography is not biometry.
_INVESTIGATION_TERMS: tuple[tuple[str, str], ...] = (
    ("oct-a", r"\bOCT[- ]?A\b|\bangio[- ]?OCT\b|\bOCT angiograph\w*"),
    ("oct", r"\bOCT\b|\boptical coherence tomograph\w*"),
    ("faf", r"\bFAF\b|\bauto[- ]?fluorescen\w*"),
    ("ffa", r"\bFFA\b|\bfluorescein angiograph\w*"),
    ("icg", r"\bICG\b|\bindocyanine\w*"),
    ("fundus_photo", r"\bfundus (?:photo\w*|image\w*)|\bcolour fundus\b|\bretinal photograph\w*"),
    ("topography", r"\btopograph\w*|\bpentacam\b|\banterion\b|\btomograph\w*"),
    ("biometry", r"\bbiometry\b|\bA[- ]?scan\b|\bIOL master\b"),
    ("pachymetry", r"\bpachymetry\b"),
    ("specular", r"\bspecular\w*"),
    ("ubm", r"\bUBM\b|\bultrasound biomicroscop\w*"),
    ("bscan", r"\bB[- ]?scan\b"),
    ("visual_field", r"\bvisual field\w*|\bperimetry\b|\bHumphrey\b|\bHVF\b|\bGoldmann field\w*"),
    ("erg", r"\bERG\b|\belectroretinogram\w*|\bEOG\b"),
    ("ct", r"\bCT\b|\bcomputed tomograph\w*"),
    ("mri", r"\bMRI\b|\bmagnetic resonance\w*"),
    ("xray", r"\bx[- ]?ray\b|\bradiograph\w*|\bchest film\b"),
    ("hess", r"\bHess\b|\bLees screen\b"),
    ("gonio", r"\bgonioscop\w*|\bgonio\b"),
    ("slitlamp_photo", r"\bslit[- ]?lamp (?:photo\w*|image\w*)|\banterior segment photo\w*"),
)
_COMPILED_TERMS = tuple((name, re.compile(pat, re.I)) for name, pat in _INVESTIGATION_TERMS)


def named_investigations(*texts: str | None) -> set[str]:
    """Every distinct investigation these texts name."""
    blob = " ".join(t for t in texts if t)
    return {name for name, pattern in _COMPILED_TERMS if pattern.search(blob)}


def classify_prompt(
    prompt: dict[str, Any], shown: dict[int, str]
) -> tuple[str, list[int], set[str]]:
    """What, if anything, is wrong with this question. Pure - no database.

    `shown` maps the id of every figure the candidate will really see to the
    words describing it. Returns the mode, the ids on screen for this question,
    and what the question names that is not there.

    The test is one thing, not two: does the question name an investigation the
    candidate cannot see? That covers a question promising three images and
    given two, and a question promising a CT and given an MRI, because both are
    the same failure - the wording claims something the screen does not have.
    """
    text = str(prompt.get("text") or "")
    wanted = str(prompt.get("image_wanted") or "").strip()
    ids = prompt.get("figure_ids") or (
        [prompt["figure_id"]] if prompt.get("figure_id") else []
    )
    here = [i for i in ids if i in shown]

    # A question that neither asks for an image nor claims to show one is fine
    # however many figures the station has.
    #
    # `image_impossible` counts as asking. It is set when sourcing has already
    # judged that no search will ever satisfy the request - "a result to be
    # read, not an image" - which is a stronger statement than the wording
    # test, and it catches what that test cannot: "This is her Quantiferon Gold
    # result" names no modality the regex knows, so on words alone it reads as
    # a question that never wanted a picture.
    if (
        not wanted
        and not prompt.get("image_impossible")
        and not PRESENTS_INVESTIGATION_RE.search(text)
    ):
        return UNCHANGED, here, set()

    if not here:
        return STATE, here, named_investigations(text, wanted)

    missing = named_investigations(text, wanted) - named_investigations(
        *(shown[i] for i in here)
    )
    return (TRIM if missing else UNCHANGED), here, missing


def _describe_what_is_there(db: Session, station: OsceStation, ids: list[int]) -> str:
    if not ids:
        return "(nothing is on screen)"
    rows = db.execute(
        select(OsceFigure.caption, OsceFigure.wanted_description).where(
            OsceFigure.id.in_(ids)
        )
    ).all()
    return "; ".join(
        (caption or wanted or "an unlabelled image") for caption, wanted in rows
    )


def reconcile_station(
    db: Session, client: AIClient, station: OsceStation, job_id: int | None = None
) -> dict[str, int]:
    """Bring every question on one station into line with what is on screen."""
    prompts = [dict(p) for p in (station.prompts or [])]
    if not prompts:
        return {"trimmed": 0, "stated": 0, "expected": 0, "unchanged": 0, "failed": 0}

    shown = _shown_figures(db, station)
    tally = {"trimmed": 0, "stated": 0, "expected": 0, "unchanged": 0, "failed": 0}
    changed = False

    for prompt in prompts:
        mode, here, missing = classify_prompt(prompt, shown)
        if mode == UNCHANGED:
            tally["unchanged"] += 1
            continue

        user = (
            f"MODE: {mode}\n"
            f"SUBSPECIALTY: {station.subspecialty or 'unknown'}\n"
            f"CASE: {station.case_summary or 'not recorded'}\n"
            f"DIAGNOSIS: {station.diagnosis or 'not recorded'}\n"
            f"FINDINGS RECORDED FOR THIS STATION:\n"
            f"{station.findings_elicited or station.findings or '(none recorded)'}\n\n"
            f"THE QUESTION AS IT STANDS:\n{prompt.get('text')}\n\n"
            f"IT ASKED TO BE SHOWN: {prompt.get('image_wanted') or '(nothing)'}\n"
            f"WHAT IS REALLY ON SCREEN: {_describe_what_is_there(db, station, here)}\n"
            f"NAMED BUT NOT ON SCREEN: {', '.join(sorted(missing)) or '(nothing)'}"
        )
        try:
            data = client.complete_json(
                task="utility", system=SYSTEM_PROMPT, user=user, job_id=job_id
            )
        except Exception as exc:  # noqa: BLE001 - one question must not stop the station
            db.rollback()
            logger.exception("Could not reconcile %s on station %s",
                             prompt.get("label"), station.id)
            log_error(db, source="osce_reconcile", message=str(exc),
                      context={"station_id": station.id, "prompt": prompt.get("label")})
            tally["failed"] += 1
            continue

        new_text = (data or {}).get("text") if isinstance(data, dict) else None
        basis = (data or {}).get("basis") if isinstance(data, dict) else None
        if not new_text or not str(new_text).strip():
            tally["failed"] += 1
            continue

        # Kept so a later run can tell a question it has already corrected from
        # one that was written this way, so an admin can see why it changed, and
        # so a bad rewrite can be put back. A model writes the replacement; the
        # original is the only copy of what the examiner report actually said.
        prompt["reconciled"] = {
            "mode": mode, "basis": basis, "shown": len(here),
            "missing": sorted(missing),
            "original": prompt.get("text"),
            "original_image_wanted": prompt.get("image_wanted"),
        }
        prompt["text"] = str(new_text).strip()
        if mode == TRIM:
            # The request now matches the wording, or the next sourcing run
            # would go looking for the images this question no longer mentions.
            kept = _describe_what_is_there(db, station, here)
            prompt["image_wanted"] = kept
            prompt["figure_ids"] = here
            prompt["figure_id"] = here[0]
            tally["trimmed"] += 1
        else:
            # Nothing is on screen and the question no longer claims otherwise,
            # so it must stop asking for an image - otherwise every future run
            # pays to search for one it has been told does not exist.
            prompt.pop("image_wanted", None)
            prompt.pop("figure_id", None)
            prompt.pop("figure_ids", None)
            tally["expected" if basis == "expected" else "stated"] += 1
        changed = True

    if changed:
        station.prompts = prompts
        flag_modified(station, "prompts")
        db.commit()
    return tally


@register_handler(JOB_RECONCILE_QUESTIONS)
def handle_reconcile_questions(ctx: JobContext) -> bool:
    """One station per chunk."""
    station_ids: list[int] = ctx.payload.get("station_ids") or []
    if not station_ids:
        raise JobHandlerError("No station_ids supplied")
    if not ctx.job.total_steps:
        ctx.set_total(len(station_ids))

    index = ctx.cursor_get("index", 0)
    if index >= len(station_ids):
        return True

    if ctx.cancelled:
        return True

    station = ctx.db.get(OsceStation, station_ids[index])
    if station is not None:
        tally = reconcile_station(ctx.db, AIClient(ctx.db), station, job_id=ctx.job.id)
        running = dict(ctx.job.result or {})
        for key, value in tally.items():
            running[key] = running.get(key, 0) + value
        ctx.set_result(**running)

    ctx.cursor_set(index=index + 1)
    ctx.advance(1, f"Checked {index + 1} of {len(station_ids)} stations")
    return index + 1 >= len(station_ids)
