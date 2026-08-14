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
from app.services.osce.station_images.verify import leaked_term
from app.services.osce.prompts import PRESENTS_INVESTIGATION_RE

logger = logging.getLogger(__name__)

JOB_RECONCILE_QUESTIONS = "reconcile_station_questions"

TRIM = "trim"
STATE = "state"
# A question restated because nothing was on screen, which now has something.
RESTORE = "restore"
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

Two things a stated result must never do.

It must not then ask the candidate to describe it. Once you have said what the \
test showed, "describe what it shows" is a question you have already answered, \
and the marks for reading it are handed over. Ask what it MEANS instead - its \
significance, what it makes you think, what you would do about it.

It must not name the diagnosis, or any word only the diagnosis supplies. The \
candidate is asked for that later in the station and would simply read it back. \
State the appearance, never the conclusion: "a choroidal neovascular membrane \
with intraretinal fluid" is a finding; "her multifocal choroiditis" is the \
answer to a question further down.

If the station's record does not say what that test showed, DO NOT \
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
    prompt: dict[str, Any],
    shown: dict[int, str],
    opening_ids: frozenset[int] = frozenset(),
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
    # Step 1 is answered by whatever the station opens on, not by a binding
    # of its own - nothing else has ever asked a question to name every image
    # on screen individually. Station 354's "Here are some images of a
    # 50-year-old male patient. Please describe what they show" had nine real
    # photographs from the paper, none of them bound to it, and this read as
    # a blank screen - so the rewrite it was given kept being told "nothing
    # is on screen" while nine real images sat there unclaimed.
    if not ids and prompt.get("step") == 1:
        ids = list(opening_ids)
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

    was_restated = (prompt.get("reconciled") or {}).get("mode") == STATE

    if not here:
        # A question already restated as what the candidate would expect is
        # left alone while nothing has arrived for it. Without this it would be
        # rewritten on every run, and each rewrite overwrites the record of the
        # one before - which is how six questions lost the request they were
        # written with.
        if was_restated:
            return UNCHANGED, here, set()
        return STATE, here, named_investigations(text, wanted)

    missing = named_investigations(text, wanted) - named_investigations(
        *(shown[i] for i in here)
    )

    # It was restated because nothing was on screen, and something now is. The
    # restatement has to go whatever arrived: station 201 was rewritten to say
    # "her corneal topography shows approximately 2 dioptres of regular
    # astigmatism", and with the topography displayed beside it the candidate
    # is shown the picture and told the answer, then asked to describe it.
    #
    # Restoring and trimming are not alternatives, which is what a first
    # attempt got wrong. 201 named topography AND pachymetry, only the
    # topography bound, so `missing` was not empty and it was trimmed instead -
    # and a trim rewrites the clause naming the images while leaving the stated
    # finding exactly where it was. The statement has to come out first; what
    # the restored wording then over-promises is an ordinary trim, and the
    # caller runs it in the same pass.
    if was_restated:
        return RESTORE, here, missing

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


# "Describe what they show" after a sentence saying what they show.
_ASKS_FOR_THE_DESCRIPTION = re.compile(
    r"\b(describe|talk me through|what (do|does) (they|it|these|this) show"
    r"|what can you see|tell me what you see)\b",
    re.I,
)


def _diagnosis_phrases(station: OsceStation) -> set[str]:
    """Adjacent word pairs from the diagnosis that actually identify it.

    A shared *word* is the subject of the question; a shared *phrase* is its
    answer. "How would you differentiate involutional from cicatricial
    ectropion?" and "Please examine the extraocular movements" were both
    rejected by a word-level test, on "ectropion" and "extraocular" - the very
    words the candidate is being asked to work with. "Multifocal choroiditis"
    and "optic disc drusen" are conclusions, and they only appear as phrases.
    """
    from app.services.osce.station_images.verify import _DIAGNOSIS_BOILERPLATE

    # Adjacency is the whole signal, so a dropped word must BREAK the pair
    # rather than close it up: filtering first turned "third nerve palsy" into
    # "third palsy" and "drusen with ..." into "drusen with", neither of which
    # anyone would ever write.
    phrases: set[str] = set()
    tokens = re.split(r"[^a-z']+", (station.diagnosis or "").lower())
    for a, b in zip(tokens, tokens[1:]):
        if len(a) <= 3 or len(b) <= 3:
            continue
        if a in _CONNECTIVES or b in _CONNECTIVES:
            continue
        if a in _DIAGNOSIS_BOILERPLATE or b in _DIAGNOSIS_BOILERPLATE:
            continue
        # Pure anatomy is a location, not a conclusion. One pathology word in
        # the pair is enough: "disc drusen" identifies, "upper eyelid" does not.
        if a in _ANATOMY and b in _ANATOMY:
            continue
        phrases.add(f"{a} {b}")
    return phrases


# Words that join two ideas rather than belonging to either. A pair spanning
# one of these is not a phrase anybody says.
_CONNECTIVES = {
    "with", "without", "secondary", "from", "following", "post", "plus",
    "associated", "causing", "complicated", "status", "versus",
}


# Where, not what. A question must be able to say which part of the eye it is
# about: "give me a differential for the left upper eyelid lesion you
# described" was rejected against a diagnosis of left upper eyelid carcinoma,
# on the words naming the eyelid. The pathology in that diagnosis is "squamous
# cell carcinoma", and it survives this list intact.
_ANATOMY = {
    "eyelid", "eyelids", "lower", "upper", "left", "right", "bilateral",
    "both", "canthus", "canthal", "medial", "lateral", "cornea", "corneal",
    "conjunctiva", "conjunctival", "iris", "lens", "retina", "retinal",
    "macula", "macular", "choroid", "choroidal", "optic", "nerve", "disc",
    "orbit", "orbital", "anterior", "posterior", "segment", "chamber",
    "angle", "vitreous", "sclera", "scleral", "eye", "eyes", "fundus",
    "peripheral", "central", "superior", "inferior", "nasal", "temporal",
}


# Naming the diagnosis as one of several candidates is the question, not the
# answer. "How would you differentiate involutional from cicatricial ectropion?"
# gives nothing away - the candidate must still say which, and why - and it is
# the form the college's own reports use most.
_OFFERS_ALTERNATIVES = re.compile(
    r"\b(differentiate|distinguish|tell apart|versus|vs\.?)\b", re.I
)


def _names_the_conclusion(text: str, station: OsceStation) -> bool:
    if _OFFERS_ALTERNATIVES.search(text):
        return False
    lowered = " ".join(re.findall(r"[a-z']+", text.lower()))
    return any(phrase in lowered for phrase in _diagnosis_phrases(station))


def _states_more_than_it_asks(
    text: str, station: OsceStation, before_the_reveal: bool = True
) -> str | None:
    """Why a rewritten question gives away what it was set to test, if it does.

    Thirteen questions came back stating the finding and then asking the
    candidate to describe it - the reading marks handed over in the stem - and
    one opened by naming the diagnosis the station reveals two questions later.

    The lenient guard, not the strict one, and only ahead of the reveal. The
    strict guard refuses a diagnosis word however it is grounded, which on a
    first pass rejected "The diagnosis is a third nerve palsy. What would you
    expect her CT angiography to show?" - the reveal question itself, doing
    exactly what the arc asks of it.
    """
    if before_the_reveal and _names_the_conclusion(text, station):
        return "it names the diagnosis the station has not revealed yet"

    # Everything before the candidate is asked to describe something is the
    # examiner talking. Splitting on the question mark instead missed the
    # commonest form of all - "Talk me through what it shows." ends in a full
    # stop, so the whole question read as stem and nothing was ever checked.
    ask = _ASKS_FOR_THE_DESCRIPTION.search(text)
    if ask:
        stem = text[: ask.start()].lower()
        # Only where the stem actually carries a finding. "This is her OCT.
        # Describe what it shows" states nothing and is the normal wording for
        # a question that really does have a picture.
        if any(w in stem for w in ("shows", "showed", "showing", "demonstrates",
                                   "reveals", "revealed", "there is", "there are")):
            return "it states the result and then asks the candidate to describe it"
    return None


# "Three differential diagnoses for this patient's presentation" - of WHAT?
_DIFFERENTIAL_OF_NOTHING = re.compile(
    r"differential(?:\s+diagnos\w+|s)?\s+for\s+(?:this|the)\s+patient's"
    r"(?:\s+current)?\s+presentation",
    re.IGNORECASE,
)

# Naming the sign and asking what produced it is the pattern the arc asks for,
# not a leak: "what is your differential for the CAUSE of this optic
# neuropathy" is a proper question even on a station whose diagnosis is an
# optic neuropathy.
_ASKS_WHAT_CAUSED_IT = re.compile(
    r"\bcause[sd]?\b|\baetiolog\w+|\betiolog\w+|\bunderlying\b"
    r"|\bsecondary to\b|\bresponsible for\b",
    re.IGNORECASE,
)

UNLEAK_PROMPT = """\
You are a RANZCO examiner correcting one question of an OSCE station that gives \
away its own answer, or asks for something the candidate cannot answer.

Rewrite it so it asks the same thing about the same case, without the fault.

If it names the diagnosis: the station reveals that later and asks the \
candidate to reach it themselves, so refer to the appearance instead of the \
conclusion.

If it asks for a differential without saying what of: say what of, taking the \
subject from the findings recorded below - "three differentials for the cause \
of the corneal melt", "your differential for this patient's reduced vision", \
"what else could produce this optic disc appearance". Never leave it as "this \
patient's presentation", which names nothing.

Say the finding instead of the conclusion. "Summarise your findings and give me \
three differential diagnoses for multifocal choroiditis" becomes "Summarise your \
findings and give me three differential diagnoses for the multifocal chorioretinal \
lesions you have described". "What other complications of Retinitis Pigmentosa \
would you look for?" becomes "What other complications of this condition would \
you look for?".

Everything else must survive unchanged: what is being asked, the clinical \
content, the number of differentials, any image the question hands over.

Never invent a finding the station did not record, and never replace the \
diagnosis with a different one.

Return JSON only:
{"text": "<the rewritten question>"}"""


def unleak_station(
    db: Session, client: AIClient, station: OsceStation, job_id: int | None = None
) -> dict[str, int]:
    """Rewrite questions that answer themselves, keeping their marks.

    Separate from `reconcile_station` because the two repair different things.
    Reconciling is about the picture: a question whose image never arrived. A
    question can hold every image it asked for and still hand over the answer
    in its wording, and that one is returned "unchanged" by every image-shaped
    check there is - which is how twenty of them stayed live.
    """
    prompts = [dict(p) for p in (station.prompts or [])]
    tally = {"unleaked": 0, "unleak_failed": 0}
    changed = False

    for prompt in prompts:
        step = int(prompt.get("step") or 0)
        text = str(prompt.get("text") or "")

        # A differential of nothing. "Three differential diagnoses for this
        # patient's presentation" is the sentence a candidate cannot answer,
        # because nothing in it says what the differential is OF.
        no_subject = bool(_DIFFERENTIAL_OF_NOTHING.search(text))

        # The diagnosis given away before the station reveals it. Two patterns
        # are deliberately NOT this: the standing instruction naming the region
        # to examine ("Please examine the cranial nerves" against a diagnosis
        # of cranial nerve palsy), and naming the sign to ask what caused it,
        # which is what the arc asks for. Both were flagged by an earlier
        # version, and rewriting them would have made good questions worse.
        early = (
            1 < step < 5
            and _names_the_conclusion(text, station)
            and not _ASKS_WHAT_CAUSED_IT.search(text)
        )
        if not (no_subject or early):
            continue

        fault = (
            "It asks for a differential without saying what the differential is "
            "OF, which the candidate cannot answer."
            if no_subject else
            "It names the diagnosis, which this station does not reveal until "
            "later."
        )
        user = (
            f"DIAGNOSIS THE STATION REVEALS LATER: {station.diagnosis or 'not recorded'}\n"
            f"FINDINGS RECORDED FOR THIS STATION:\n"
            f"{station.findings_elicited or station.findings or '(none recorded)'}\n\n"
            f"WHAT IS WRONG WITH THIS QUESTION: {fault}\n\n"
            f"THE QUESTION AS IT STANDS:\n{text}"
        )
        try:
            data = client.complete_json(
                task="utility", system=UNLEAK_PROMPT, user=user, job_id=job_id
            )
        except Exception:  # noqa: BLE001 - one question must not stop the station
            db.rollback()
            logger.exception("Could not unleak %s on station %s",
                             prompt.get("label"), station.id)
            tally["unleak_failed"] += 1
            continue

        new_text = (data or {}).get("text") if isinstance(data, dict) else None
        # The replacement has to actually be an improvement. A rewrite that
        # still names the conclusion, or that answers itself another way, is
        # not worth the original wording.
        replacement = str(new_text or "").strip()
        still_wrong = (
            not replacement
            # The fault it was sent to fix, still there.
            or bool(_DIFFERENTIAL_OF_NOTHING.search(replacement))
            or (
                _names_the_conclusion(replacement, station)
                and not _ASKS_WHAT_CAUSED_IT.search(replacement)
            )
            # Or a new fault in its place: stating the result and then asking
            # the candidate to describe it.
            or _states_more_than_it_asks(replacement, station, False)
        )
        if still_wrong:
            tally["unleak_failed"] += 1
            continue

        previous = prompt.get("reconciled") or {}
        prompt["reconciled"] = {
            **previous,
            "mode": "unleak",
            "original": previous.get("original") or text,
        }
        prompt["text"] = replacement
        tally["unleaked"] += 1
        changed = True

    if changed:
        station.prompts = prompts
        flag_modified(station, "prompts")
        db.commit()
    return tally


def reconcile_station(
    db: Session, client: AIClient, station: OsceStation, job_id: int | None = None
) -> dict[str, int]:
    """Bring every question on one station into line with what is on screen."""
    prompts = [dict(p) for p in (station.prompts or [])]
    if not prompts:
        return {"trimmed": 0, "stated": 0, "expected": 0, "unchanged": 0, "failed": 0}

    from app.services.osce.sittability import (
        answers_a_view,
        opening_figures,
    )

    shown = _shown_figures(db, station)
    opening_ids = frozenset(
        f.id for f in opening_figures(station) if answers_a_view(f) and f.id in shown
    )
    tally = {"trimmed": 0, "stated": 0, "expected": 0, "restored": 0,
             "unchanged": 0, "failed": 0}
    changed = False

    for prompt in prompts:
        mode, here, missing = classify_prompt(prompt, shown, opening_ids)
        if mode == UNCHANGED:
            tally["unchanged"] += 1
            continue

        if mode == RESTORE:
            # Deterministic: the wording it had before the restatement is
            # stored, and it is the wording written for exactly this - a
            # question with its image present. No model is needed to put back
            # a sentence.
            record = prompt.get("reconciled") or {}
            original = record.get("original")
            if not original:
                tally["failed"] += 1
                continue
            prompt["text"] = original
            prompt.pop("image_search_exhausted", None)
            prompt.pop("reconciled", None)
            tally["restored"] = tally.get("restored", 0) + 1
            changed = True

            # The wording it had back may still name more than arrived - 201
            # asked for topography and pachymetry and was given the topography.
            # That is an ordinary trim, and doing it now rather than next run
            # means the question is never left over-promising.
            mode, here, missing = classify_prompt(prompt, shown, opening_ids)
            if mode != TRIM:
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

        # The reveal question and everything after it are entitled to the
        # diagnosis; the arc puts it there on purpose.
        before_reveal = int(prompt.get("step") or 0) < 5
        spoiled = _states_more_than_it_asks(str(new_text), station, before_reveal)
        if spoiled:
            # Both failures are the same trade gone wrong: the rewrite bought
            # a question the candidate can follow by giving away what they were
            # meant to supply. The "expected" form never can - it asks what the
            # test WOULD show - so that is what a spoiled rewrite falls back to.
            logger.info("Rewrite of %s on station %s %s; asking for the "
                        "expected form instead", prompt.get("label"), station.id,
                        spoiled)
            try:
                data = client.complete_json(
                    task="utility", system=SYSTEM_PROMPT,
                    user=user + (
                        f"\n\nYOUR PREVIOUS ATTEMPT WAS REJECTED: {spoiled}.\n"
                        f"Do not state the result at all. Ask what the candidate "
                        f"would EXPECT it to show, and answer with basis "
                        f'"expected".'
                    ),
                    job_id=job_id,
                )
            except Exception:  # noqa: BLE001
                db.rollback()
                tally["failed"] += 1
                continue
            new_text = (data or {}).get("text") if isinstance(data, dict) else None
            basis = (data or {}).get("basis") if isinstance(data, dict) else None
            if (
                not new_text
                or not str(new_text).strip()
                or _states_more_than_it_asks(str(new_text), station, before_reveal)
            ):
                tally["failed"] += 1
                continue

        # Kept so a later run can tell a question it has already corrected from
        # one that was written this way, so an admin can see why it changed, and
        # so a bad rewrite can be put back. A model writes the replacement; the
        # original is the only copy of what the examiner report actually said.
        # The FIRST original is the one worth keeping. A second rewrite used to
        # overwrite it with its own input, which is the already-rewritten text -
        # so station 201's true wording was replaced by the sentence that stated
        # its findings, and the restore that would have removed that statement
        # had nothing left to restore to. Six questions lost their image request
        # the same way.
        previous = prompt.get("reconciled") or {}
        prompt["reconciled"] = {
            "mode": mode, "basis": basis, "shown": len(here),
            "missing": sorted(missing),
            "original": previous.get("original") or prompt.get("text"),
            "original_image_wanted": (
                previous.get("original_image_wanted") or prompt.get("image_wanted")
            ),
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
            # so searching for it again would spend on an image already known
            # not to exist. That is one fact. What the question needed is
            # another, and deleting `image_wanted` to express the first
            # destroyed the second: `bind_ingested_figures_to_questions` matches
            # a question's request against the figures the station already
            # holds, and a question with no request can never be matched. It
            # cost 22 questions the chance of being given a picture from the
            # examiners' own report.
            prompt["image_search_exhausted"] = True
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
