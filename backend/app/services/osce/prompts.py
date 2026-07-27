"""Turn the flat OSCE station record into a timed examiner conversation.

The examiners' reports give each station as a case summary, findings, diagnosis
and a 20-mark rubric - but a real OSCE is a dialogue: the examiner asks, the
candidate speaks, the examiner asks the next thing. This converts a station
into that ordered sequence, splitting the rubric so every spoken answer is
marked against exactly what was asked of it.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.constants import OSCE_STATION_MARKS, OSCE_STATION_MINUTES
from app.models import OsceStation
from app.services.ai import AIClient
from app.services.ai.client import AIError
from app.services.coerce import as_float
from app.services.errors import log_error
from app.services.jobs.runner import JobContext, JobHandlerError, register_handler
from app.services.marking import absorb_mark_drift

logger = logging.getLogger(__name__)

JOB_BUILD_OSCE_PROMPTS = "build_osce_prompts"

STATION_SECONDS = OSCE_STATION_MINUTES * 60  # 540
STATION_MARKS = OSCE_STATION_MARKS
MIN_PROMPTS = 3
# How many times to ask before giving up on a station.
_GENERATE_ATTEMPTS = 2
# The arc below runs to seven steps: instruction, ancillary test, read the
# image, differentials, the diagnosis-and-management question, an evolving
# hypothetical and a knowledge question. One question per step, no more.
MAX_PROMPTS = 7

SYSTEM_PROMPT = f"""\
You are a RANZCO examiner running one station of the RACE OSCE. A station lasts \
exactly {OSCE_STATION_MINUTES} minutes ({STATION_SECONDS} seconds) and is a \
spoken dialogue: you ask a question, the candidate answers aloud, then you ask \
the next one.

You are given a station's case, findings, diagnosis and its 20-mark rubric. \
Convert it into the ordered sequence of questions you would actually ask.

THE AIMS ARE THE STATION. The aims listed below are what this station exists
to test, and they are what the examiner actually asked. EVERY aim must become
a question, in the words an examiner would use. An aim of "to distinguish
globe dystopia from vertical strabismus" is the question "How would you tell
whether that is globe dystopia or a vertical strabismus?" - ask it. An aim of
"to recognise a tectonic graft and examine its integrity" is "How would you
assess the integrity of that graft?" The arc below decides where a question
sits in the sequence; the aims decide which questions exist at all. A station
whose questions could be swapped with another station's has failed, because
the aims are what make it this station and not a generic one.

WHAT MUST NOT BE GIVEN AWAY - and only this:
- The DIAGNOSIS, before the examiner states it at step 5. Never name it, and
  never hint at it with a phrase only the diagnosis explains.
- WHERE TO LOOK, in the standing instruction at step 1. It names a region and
  an eye, never a structure or a sign.
Everything else is fair. Once the candidate has described the signs, an
examiner refers to them by name - that is how a real station sounds. "How
would you measure that proptosis?" and "What is the significance of the
anomalous vessels?" are proper questions, not leaks.

How a RANZCO station is actually built, from real examiner handouts. Use this
arc for ORDER. Steps 1, 4 and 5 are in every station, and at least one of 6
and 7; step 3 whenever the case turns on an investigation. Step 2 is the
case's own examination or test question and is almost always present. Where a
case carries more, add questions - four to seven is the normal range, and a
station with five aims needs more questions than one with two:

1. THE STANDING INSTRUCTION. The first question is always what the candidate
   is told as they walk in: the region and the eye, nothing else. "Please
   examine the posterior segment of both eyes." "Please examine the anterior
   segment of the left eye." "Please perform anterior segment examination for
   both eyes and perform retinoscopy for the right eye only." No history, no
   findings, no hint of the diagnosis, no list of structures to check, and no
   "and describe what you see" shopping list.
   It is still marked, and heavily: what comes back is the candidate's
   description of the signs, so EVERY rubric point about identifying or
   describing a finding belongs here. It must never carry zero marks.
2. WHAT ELSE WOULD YOU DO - about THIS case. This is the step that most often
   comes out generic, and a generic version is a wasted question. "What other
   investigations would you perform in this patient?" could be asked of any
   station in the college's history, tests nothing specific, and must not be
   your answer.
   Ask instead the manoeuvre, measurement or test this case turns on, in the
   examiner's words:
     "How would you measure the globe dystopia?"
     "What would you look for on gonioscopy?"
     "How would you assess the integrity of that graft?"
     "How would you work up the systemic associations of that retinal finding?"
     "What would you do to confirm the site of the lesion?"
   Take it from the aims and the rubric - they name the test the examiners
   cared about. Where the candidate genuinely should propose the test
   unprompted, ask what they would do NEXT for this specific problem rather
   than naming the test for them.
3. READ THE ANCILLARY IMAGE. Having asked for it, they describe what it shows
   - correctly naming the sign, its extent, and what is absent. Ask it blind:
   "What does this show?" / "Describe the OCT."
   Ask this step whenever the case genuinely turns on an investigation - the
   examiners' report is the guide, and a report that says candidates misread
   the MRI means the MRI was put in front of them. If the request below does
   not already list that image, ask the question anyway and describe the image
   it needs in "image_wanted": it will be sourced and verified before any
   candidate sees the station.
   "image_wanted" is a description for an image librarian, not a question:
   name the modality, the laterality and exactly what must be visible - "MRI
   of the orbits, coronal, showing an enlarged right inferior rectus muscle
   with normal other recti and no mass". Getting this precise matters, because
   a candidate is marked on describing what is actually shown.
   Never present a result you have not either been given or asked for this
   way. "This is her A-scan biometry" for a scan that does not exist leaves
   the candidate reading a blank screen.
4. SUMMARISE AND DIFFERENTIATE, with a stated number: "Can you summarise your
   findings and give 5 differential diagnoses?"
5. THE EXAMINER GIVES THE DIAGNOSIS AND ASKS FOR MANAGEMENT. This is ONE
   question, and the pair is the point of it: state the diagnosis plainly -
   "The presumed diagnosis is amelanotic iris melanoma" - and in the same
   breath ask for the plan, framed as ownership: "How would you manage him if
   he were new to you and you had just made the diagnosis?" Giving it away
   here is deliberate: a station must keep going even when the candidate has
   not got there, and later marks must not depend on earlier ones. This is the
   FIRST question in which the diagnosis may be spoken, and from here on
   naming it is expected.
6. THE CASE MOVES ON - a hypothetical that evolves it in time or severity:
   "You observe the patient for 5 years, there has been minimal change. He
   develops a cataract and vision drops to 6/18. What are your options?" /
   "If a ciliary body lesion were found on UBM, what further investigations
   would you do?" / "What if they had an opaque cornea?"
7. STRAIGHT KNOWLEDGE, off this patient entirely: criteria, inheritance,
   classification, risk factors - and ask for a number where one exists.
   "What are the criteria for keratoconus progression?" "What is the
   inheritance pattern?" "What are the risk factors for developing
   keratoconus? Name 4." "What are the types of paediatric glaucoma?"

THREE STATIONS, END TO END. Read them for how differently they sound - the
arc is the same and almost no wording is. Do NOT reuse these sentences; they
are here to show the range, and a question copied out of them is a question
that was not written for the case in front of you.

Serpiginous choroiditis with a secondary CNVM. Aims: identify multifocal
choroiditis and form differentials; interpret ancillary tests; organise tests,
referrals and a plan.
  A. (1) "Please examine the posterior segment of both eyes."
  B. (2) "Which tests would you organise to sort out the cause of that
      choroiditis?"
  C. (3) "This is her OCT and fluorescein angiogram. What do they show?"
  D. (4) "Please summarise your findings and give me 4 differential diagnoses."
  E. (5) "The diagnosis is serpiginous choroiditis with a secondary choroidal
      neovascular membrane. How would you manage her if she were new to your
      practice today?"
  F. (6) "Her Mantoux and QuantiFERON Gold come back positive. What is the
      significance of that, and what changes?"
  G. (7) "What are the causes of a serpiginous-like choroiditis? Name 4."

Orbital inflammatory disease. Aims: perform an orbital examination
systematically; distinguish globe dystopia from vertical strabismus; form
differentials; understand the principles of orbital disease management.
  A. (1) "Please examine the orbits of both eyes."
  B. (2) "How would you measure that dystopia, and how would you satisfy
      yourself it is not a vertical strabismus?"
  C. (3) "This is his MRI. Talk me through it, including the negatives."
  D. (4) "Summarise the case for me and give three differentials."
  E. (5) "This is idiopathic orbital inflammatory disease. He is yours from
      today - what is your plan?"
  F. (7) "Which systemic conditions would you want to exclude? Name three."

Advanced primary open angle glaucoma on maximal drops. Aims: assess the disc
and quantify the damage; understand target pressures; know when to escalate.
  A. (1) "Please examine the optic discs of both eyes."
  B. (2) "What would you look for on gonioscopy in this man?"
  C. (3) "These are his visual fields. Talk me through them."
  D. (4) "Summarise the case and tell me what is going on."
  E. (5) "This is advanced primary open angle glaucoma. What pressure would
      you be aiming for, and how would you get there?"
  F. (6) "He comes back in a year and the field has progressed despite a
      pressure of 14. What now?"
  G. (7) "What are the risk factors for progression in open angle glaucoma?
      Name 4."

Register, from the handouts - match it exactly:
- Short, spoken, second person, ONE thing asked at a time. Do not staple two
  questions together with "and" - "describe the findings and give your leading
  diagnosis" is two questions, and each belongs to its own step of the arc.
  Step 5 is the sole exception: there the diagnosis and the management ask are
  one question.
- "How would you confirm the diagnosis?" not "The candidate should be asked to
  confirm the diagnosis."
- Ask for a stated number wherever the answer is a list: "Name 4", "give 5
  differential diagnoses".
- Refer to the patient as a person - "How would you manage him if he were new
  to your practice?"
- Never number the questions in their text, and never preface them with
  "Question 3" or "Next". Say only what the examiner would say.
- Vary how you ask. Real examiners say "Talk me through it", "What is going on
  here?", "He is yours from today - what is your plan?", "Her mother asks
  you...", not the same five sentences at every station. Before you finish,
  read your questions back: if any of them could be pasted into a different
  station without changing a word, it is not testing this case and needs
  rewriting around the aims, the findings or the rubric.

RECOVERING THE QUESTIONS THAT WERE REALLY ASKED. For a station taken from a
past examiners' report, the report is a record of a station that actually ran,
and it says what was asked - just not in question form. Read it that way and
put the real questions back:
- The AIMS are the asks, one step removed. "To discuss vision rehabilitation
  in paediatric cataract" means the examiner asked "How would you rehabilitate
  her vision?" Every aim should be traceable to a question you write.
- WHAT THE COHORT MISSED names what was asked. "Few candidates considered the
  patient would likely need a general anaesthetic" means they were asked about
  the anaesthetic; "very few considered the regular risks of cataract surgery"
  means they were asked to consent or counsel. Turn each into the question
  that would expose it, and mark that rubric point is_critical.
- HOW THE COHORT PERFORMED tells you where the station's weight sat: what they
  did well was still asked, and still needs a question.
Then fit those recovered questions to the arc above rather than inventing
fresh ones: they are what a real examiner said, and they outrank anything you
would have thought of. Where the report gives you nothing for a step, write
the step as the arc describes it.

Other rules:
- Produce between {MIN_PROMPTS} and {MAX_PROMPTS} questions.
- Give each question the number of the arc step it came from, in "step". No
  step may appear twice, and they must be in ascending order.
- Keep the opening instruction as an examiner gives it - "Please examine..." -
  even though there is no live patient: the candidate is shown the station's
  photograph and answers from it. Later questions needing a hands-on manoeuvre
  should ask what they would look for and what it would show.
- Give each question a time in seconds. The times MUST total exactly \
{STATION_SECONDS}. Weight them by how much the question is worth.
- Split the supplied 20-mark rubric across the questions. Every rubric point \
must appear under exactly one question, reworded only if needed to read as a \
markable expectation. The marks across ALL questions must total exactly 20.
- Where the examiners noted a common mistake, make sure the question that would \
expose it is present, and mark that rubric point is_critical.

Return ONLY a JSON object:
{{
  "prompts": [
    {{"label": "A",
      "text": "the question as spoken",
      "step": <integer 1-7, the arc step this question is>,
      "image_wanted": "<only on a step 3 question that needs an image the
                       request does not already list; otherwise omit>",
      "seconds": <integer>,
      "rubric": [
        {{"text": "markable expectation", "marks": <number>,
          "is_critical": <boolean>}}
      ]}}
  ]
}}"""


def build_prompts_for_station(
    db: Session, client: AIClient, station: OsceStation, job_id: int | None = None
) -> dict[str, Any]:
    """Generate and persist the examiner prompt sequence for one station."""
    rubric_lines = "\n".join(
        f"  - {item.get('marks', 0):g} mark(s): {item.get('text', '')}"
        for item in (station.rubric or [])
    ) or "  (no rubric recorded)"

    mistakes = "\n".join(f"  - {m}" for m in (station.common_mistakes or [])) or "  (none recorded)"

    # What the report says the candidate was asked to do, where it records it
    # at all. This is the closest thing to the real wording that survives.
    tasks = "\n".join(
        f"  - {t.get('text') or t.get('task') or t}" if isinstance(t, dict) else f"  - {t}"
        for t in (station.tasks or [])
    ) or "  (not recorded)"

    # The first figure is the patient: it is what the candidate is looking at
    # when told to examine. Only a SECOND figure is an ancillary test there is
    # something to hand over and read, so only then can step 3 be asked. A
    # station shown one external photograph was being asked "this is her A-scan
    # biometry, what does it show?" - about a scan that does not exist.
    figures = list(station.figures)
    has_image = len(figures) > 1
    if not figures:
        listed = "  (none yet)"
    else:
        listed = "\n".join(
            f"  {i + 1}. {f.caption or f.wanted_description or 'unlabelled image'}"
            for i, f in enumerate(figures)
        )
    needs_investigation = station_needs_an_investigation(station.rubric)
    # What this station is about, used to reject questions that could have been
    # written for any station at all.
    vocabulary = station_vocabulary(station)
    rubric_demands = (
        "\nThe rubric marks the candidate on READING an investigation, which means "
        "one was put in front of them at the real station. Arc step 3 is therefore "
        "REQUIRED here: ask it, and give the image you need in \"image_wanted\"."
        if needs_investigation
        else ""
    )
    image_note = (
        f"IMAGES THIS STATION ALREADY HAS:\n{listed}\n"
        "The first is the patient - what the candidate examines at step 1, not "
        "something to hand over. Any others are ancillary tests you may ask about "
        "directly. If the case needs an investigation that is not listed, still ask "
        "the question and describe the image in \"image_wanted\"; it will be sourced "
        "and checked against your description before the station is used."
        + rubric_demands
    )

    user = (
        f"SUBSPECIALTY: {station.subspecialty or 'unspecified'}\n"
        f"STATION: {station.title or f'Station {station.station_number}'}\n"
        f"SOURCE: {station.source or 'unknown'}"
        + (f", {station.exam_period}" if station.exam_period else "")
        + f"\n\nCASE SUMMARY:\n{station.case_summary or '(none)'}\n\n"
        f"AIMS OF THE STATION:\n"
        + ("\n".join(f"  - {a}" for a in (station.aims or [])) or "  (none)")
        + f"\n\nPATIENT HISTORY:\n{station.patient_history or '(none)'}\n\n"
        f"EXAMINATION FINDINGS:\n{station.findings or '(none)'}\n\n"
        f"DIAGNOSIS:\n{station.diagnosis or '(none)'}\n\n"
        f"MARKING RUBRIC (20 marks total):\n{rubric_lines}\n\n"
        f"MISTAKES THE EXAMINERS NOTED IN THE REAL COHORT:\n{mistakes}\n\n"
        f"HOW THE COHORT PERFORMED:\n{station.cohort_performance or '(not recorded)'}\n\n"
        f"CANDIDATE TASKS AS THE REPORT RECORDS THEM:\n{tasks}\n\n"
        f"{image_note}\n\n"
        f"Write the examiner's question sequence now. Check your arithmetic: "
        f"seconds must total {STATION_SECONDS} and marks must total 20."
    )

    prompts, warnings = _generate(client, user, job_id)

    # The arc is the whole point of the station, and the model does drop steps
    # or give the diagnosis away in the opening instruction. Say what is wrong
    # and ask once more rather than shipping a station that examines nothing.
    problems = _arc_problems(prompts, has_image, needs_investigation, vocabulary, station.aims)
    if problems:
        retry_user = (
            user
            + "\n\nYour first attempt was rejected because:\n"
            + "\n".join(f"  - {p}" for p in problems)
            + "\n\nRewrite the whole sequence, fixing these."
        )
        retried, retry_warnings = _generate(client, retry_user, job_id)
        remaining = _arc_problems(retried, has_image, needs_investigation, vocabulary, station.aims)
        # Keep whichever attempt is closer to a real station; a second try that
        # is still imperfect is usually still better than the first.
        if len(remaining) <= len(problems):
            prompts, warnings, problems = retried, retry_warnings, remaining
        if problems:
            warnings.append("Question arc is incomplete: " + "; ".join(problems))

    station.prompts = prompts
    station.prompts_status = "complete"
    meta_warnings = warnings
    db.commit()
    return {"prompts": len(prompts), "warnings": meta_warnings}


def _generate(
    client: AIClient, user: str, job_id: int | None
) -> tuple[list[dict[str, Any]], list[str]]:
    """One round trip, normalised. Raises if nothing usable comes back.

    The utility model intermittently answers with a stub - one station came
    back as the twelve characters '{\\n  "prompts' - which the repair pass
    inside complete_json cannot mend, because there is nothing there to repair.
    Asking again gets a full answer, so a station is not lost to a bad draw.
    """
    for attempt in range(_GENERATE_ATTEMPTS):
        try:
            data = client.complete_json(
                task="utility", system=SYSTEM_PROMPT, user=user, job_id=job_id
            )
            break
        except AIError:
            if attempt + 1 >= _GENERATE_ATTEMPTS:
                raise
            logger.warning("Prompt generation returned nothing usable; asking again")

    if isinstance(data, list):
        data = {"prompts": data}
    if not isinstance(data, dict):
        raise ValueError("Prompt generation did not return a JSON object")

    prompts, warnings = _normalise(data.get("prompts") or [])
    if not prompts:
        raise ValueError("No usable prompts were produced")
    return prompts, warnings


# Openings that have crept in and give the game away: they tell the candidate
# which structure is abnormal, or hand them a checklist to describe.
_OPENING_GIVEAWAYS = (
    "describe what you see",
    "describe the findings",
    "what you would look for",
    "including",
    "making sure",
    "paying attention",
)

_REQUIRED_STEPS = (1, 2, 4, 5)

# Investigations a candidate is marked on READING, as opposed to merely naming.
# A rubric point about describing MRI findings is proof the scan was put in
# front of them, so the station has to put it in front of them too.
_INVESTIGATIONS = re.compile(
    r"\b(mri|ct\b|oct\b|angiogram|angiography|ffa\b|icg\b|ultrasound|b-?scan|"
    r"ubm\b|biometry|visual fields?|perimetry|topography|specular|pachymetry|"
    r"electroretinogram|erg\b|photograph)\b",
    re.IGNORECASE,
)
# ...but only when the point is about interpreting one, not ordering one.
_READS_IT = re.compile(
    r"\b(describ|identif|interpret|recognis|recogniz|read|comment)", re.IGNORECASE
)


# Words that carry no clinical content: shared by every station, so they can
# never be evidence that a question was written for this one.
_GENERIC_WORDS = frozenset("""
about would could should there their this that these those with from your yours
what when where which whom while have here does doing done been being
patient patients examine examination examining examiner candidate station
please describe description discuss discussion understand understanding
finding findings sign signs perform performing formulate appropriate suitable
relevant important management manage managing plan plans principle principles
consider considering consideration approach investigate investigation
investigations test tests other others next then also based presentation
present presents give given tell take takes make makes want need needs
question questions answer answers eye eyes both left right case cases
diagnosis diagnoses differential differentials summarise summarize summary
ancillary interpret interpreting interpretation organise organize referral
referrals systematic systematically recognise recognize identify identifying
demonstrate demonstrating knowledge suitable comment comments elicit
""".split())


def _content_words(text: str) -> set[str]:
    """The words in `text` that could only come from a particular case."""
    return {
        word
        for word in re.findall(r"[a-z]+", (text or "").lower())
        if len(word) > 3 and word not in _GENERIC_WORDS
    }


def station_vocabulary(station: OsceStation) -> set[str]:
    """Everything this station is about, as words a question could echo."""
    parts = [
        station.findings or "",
        station.findings_elicited or "",
        station.diagnosis or "",
        station.case_summary or "",
        station.subspecialty or "",
        " ".join(station.aims or []),
        " ".join(str(p.get("text") or "") for p in (station.rubric or [])),
        " ".join(station.common_mistakes or []),
    ]
    return _content_words(" ".join(parts))


def station_needs_an_investigation(rubric: list[dict[str, Any]] | None) -> bool:
    """Does the marking rubric expect the candidate to READ an investigation?"""
    for point in rubric or []:
        text = str(point.get("text") or "")
        if _INVESTIGATIONS.search(text) and _READS_IT.search(text):
            return True
    return False

# Handing over a result: "this is her A-scan biometry", "here is the OCT".
# Harmless when the image exists and fatal when it does not, because the
# candidate is asked to read something the screen never shows them.
_PRESENTS_A_RESULT = re.compile(
    r"\b(this is|here is|here are|these are|shown here|i am showing you|"
    r"what does (this|it) show|what do these show)\b",
    re.IGNORECASE,
)


def _generic_problems(
    prompts: list[dict[str, Any]],
    vocabulary: set[str] | None,
    aims: list[str] | None,
) -> list[str]:
    """Catch a sequence that would fit any station, and aims never asked about.

    Measured over 36 rebuilt stations, step 2 came back as the same sentence -
    "What other investigations would you perform in this patient?" - 29 times.
    A question that shares no word with its own case is not testing that case,
    and an aim with no question is a piece of the station simply missing.
    """
    problems: list[str] = []
    if not vocabulary:
        return problems

    # Steps 1 and 4 are formulaic in the real exam too: "Please examine the
    # posterior segment of both eyes" is exactly what is said. The middle and
    # late questions are where a station has to be itself.
    for prompt in prompts:
        if prompt.get("step") in (1, 4) or not prompt.get("step"):
            continue
        if not (_content_words(prompt["text"]) & vocabulary):
            problems.append(
                f"question {prompt['label']} ({prompt['text'][:60]!r}...) says nothing "
                "specific to this case - it would fit any station; ask what this case's "
                "aims and rubric actually turn on"
            )

    asked = _content_words(" ".join(p["text"] for p in prompts))
    for aim in aims or []:
        wanted = _content_words(aim) & vocabulary
        # An aim like "Describe findings." carries nothing to check against.
        if len(wanted) < 2:
            continue
        if not (wanted & asked):
            problems.append(
                f"the aim {aim[:70]!r} is never asked about; it is what the station "
                "exists to test, so it needs a question of its own"
            )
    return problems


def _arc_problems(
    prompts: list[dict[str, Any]],
    has_image: bool = True,
    needs_investigation: bool = False,
    vocabulary: set[str] | None = None,
    aims: list[str] | None = None,
) -> list[str]:
    """Check the sequence against the arc. Empty means it is a real station."""
    problems: list[str] = []
    steps = [p.get("step") for p in prompts]
    problems.extend(_generic_problems(prompts, vocabulary, aims))

    # Step 3 reads an ancillary image. The station need not already have one -
    # a question that asks for it says what it needs and the image is sourced -
    # so it is required when there is one to read, or when the rubric marks the
    # candidate on reading one.
    required = (
        (*_REQUIRED_STEPS, 3) if has_image or needs_investigation else _REQUIRED_STEPS
    )
    for step in sorted(required):
        if steps.count(step) != 1:
            problems.append(
                f"arc step {step} appears {steps.count(step)} times; it must appear exactly once"
            )
    if not any(s in (6, 7) for s in steps):
        problems.append("neither a hypothetical (step 6) nor a knowledge question (step 7) is present")

    ordered = [s for s in steps if isinstance(s, int)]
    if ordered != sorted(ordered):
        problems.append("the questions are not in arc order")

    # Nothing may be handed over that the candidate will not actually see. A
    # question that presents a result must either be reading an image the
    # station already has, or have said which image to go and find.
    for index, prompt in enumerate(prompts):
        # "This is idiopathic orbital inflammatory disease" is step 5 stating
        # the diagnosis, not an image being handed over. Only a sentence that
        # presents something of a MODALITY needs an image behind it.
        if not (
            _PRESENTS_A_RESULT.search(prompt["text"])
            and _INVESTIGATIONS.search(prompt["text"])
        ):
            continue
        reads_existing = has_image and prompt.get("step") == 3
        if not reads_existing and not prompt.get("image_wanted"):
            problems.append(
                f"question {prompt['label']} presents a test result "
                f"({prompt['text'][:60]!r}...) with no image to show for it; either "
                "describe the image it needs in image_wanted or ask which test they "
                "would order instead"
            )
            break
        if index == 0:
            problems.append("the standing instruction cannot hand over a test result")
            break

    opening = prompts[0]
    if opening.get("step") != 1:
        problems.append("the first question is not the standing instruction")
    else:
        lowered = opening["text"].lower()
        for phrase in _OPENING_GIVEAWAYS:
            if phrase in lowered:
                problems.append(
                    f"the standing instruction says {phrase!r}; it must give the region and "
                    "the eye and nothing else"
                )
                break
        if not any(pt["marks"] > 0 for pt in opening["rubric"]):
            problems.append(
                "the standing instruction carries no marks; every rubric point about "
                "identifying or describing a finding belongs to it"
            )

    return problems


def _normalise(raw_prompts: list[Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Coerce to the stored shape and force the timing and marks to add up."""
    warnings: list[str] = []
    prompts: list[dict[str, Any]] = []

    for index, item in enumerate(raw_prompts):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue

        rubric: list[dict[str, Any]] = []
        for point in item.get("rubric") or []:
            if not isinstance(point, dict) or not point.get("text"):
                continue
            rubric.append(
                {
                    "text": str(point["text"]).strip(),
                    "marks": as_float(point.get("marks"), 0.0),
                    "is_critical": bool(point.get("is_critical")),
                }
            )

        prompts.append(
            {
                "label": str(item.get("label") or chr(ord("A") + index)).strip(),
                "text": text,
                # Which step of the examiner's arc this is; used to check the
                # sequence actually examines the candidate, and kept so a
                # station can be audited later.
                "step": int(as_float(item.get("step"), 0.0) or 0) or None,
                # What image this question needs but the station does not yet
                # have. Sourcing turns it into a figure bound to this question.
                "image_wanted": str(item.get("image_wanted") or "").strip() or None,
                "seconds": max(15, int(as_float(item.get("seconds"), 0.0) or 0)),
                "rubric": rubric,
            }
        )

    if not prompts:
        return [], warnings

    # Timing must fill the station exactly; the candidate is entitled to the
    # full nine minutes and not a second more.
    total_seconds = sum(p["seconds"] for p in prompts)
    if total_seconds != STATION_SECONDS:
        warnings.append(f"Prompt times totalled {total_seconds}s; rescaled to {STATION_SECONDS}s.")
        factor = STATION_SECONDS / total_seconds
        for prompt in prompts:
            prompt["seconds"] = max(15, int(round(prompt["seconds"] * factor)))
        drift = STATION_SECONDS - sum(p["seconds"] for p in prompts)
        prompts[-1]["seconds"] = max(15, prompts[-1]["seconds"] + drift)

    # Marks must total 20, for the same reason they must in a written paper.
    total_marks = sum(pt["marks"] for p in prompts for pt in p["rubric"])
    if total_marks > 0 and abs(total_marks - STATION_MARKS) > 0.01:
        warnings.append(
            f"Rubric totalled {total_marks:g} marks; rescaled to {STATION_MARKS}."
        )
        factor = STATION_MARKS / total_marks
        for prompt in prompts:
            for point in prompt["rubric"]:
                point["marks"] = round(point["marks"] * factor, 2)
        absorb_mark_drift([pt for p in prompts for pt in p["rubric"]], STATION_MARKS)
    elif total_marks == 0:
        warnings.append("No rubric marks were produced for this station.")

    return prompts, warnings


# --- Job handler ----------------------------------------------------------
@register_handler(JOB_BUILD_OSCE_PROMPTS)
def handle_build_osce_prompts(ctx: JobContext) -> bool:
    """Build prompt sequences, one station per chunk."""
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
            outcome = build_prompts_for_station(
                ctx.db, AIClient(ctx.db), station, job_id=ctx.job.id
            )
            done = list((ctx.job.result or {}).get("completed", []))
            done.append(station.id)
            ctx.set_result(completed=done)
            if outcome["warnings"]:
                warned = list((ctx.job.result or {}).get("warnings", []))
                warned.extend(f"Station {station.station_number}: {w}" for w in outcome["warnings"])
                ctx.set_result(warnings=warned)
        except Exception as exc:  # noqa: BLE001 - one station must not stop the batch
            ctx.db.rollback()
            logger.exception("Prompt build failed for station %s", station.id)
            log_error(
                ctx.db, source="osce_prompts", message=str(exc),
                context={"station_id": station.id},
            )
            station.prompts_status = "failed"
            ctx.db.commit()
            failed = list((ctx.job.result or {}).get("failed", []))
            failed.append(station.id)
            ctx.set_result(failed=failed)

    ctx.cursor_set(index=index + 1)
    ctx.advance(1, f"Stations prepared: {index + 1} of {len(station_ids)}")
    return index + 1 >= len(station_ids)


def stations_needing_prompts(db: Session) -> list[int]:
    return list(
        db.execute(
            select(OsceStation.id)
            .where(OsceStation.prompts_status.in_(["none", "failed"]))
            .order_by(OsceStation.id)
        ).scalars().all()
    )

