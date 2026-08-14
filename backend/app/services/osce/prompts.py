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
from types import SimpleNamespace

from app.services.marking import absorb_mark_drift, rescale_marks_to_awardable

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

THE COLLEGE'S WORDS FOR TESTS. Two categories, and they are not the same:
- ANCILLARY TESTS are done in the clinic, on the day: OCT, fluorescein
  angiography, corneal topography, visual fields, biometry, specular
  microscopy, B-scan ultrasound.
- INVESTIGATIONS are done outside the clinic: bloods, serology, genetic
  testing, CT, MRI, chest X-ray.
Use whichever word is right for what you are asking about. "What ancillary
tests would help?" and "What investigations would you order?" are different
questions with different answers, and a candidate sitting this exam is marked
by examiners who use the terms this way.

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
arc for ORDER, NOT as a form to fill in.

Only two steps belong in every station: step 1, because a station has to open
with an instruction, and step 5, because stating the diagnosis midway is what
lets a candidate who has not got there still earn the later marks. Every other
step has to be earned by THIS report. Include a step when the aims and the
rubric show it is that kind of case, and leave it out when they do not.

A step included because the arc has a slot for it produces a question nobody
can answer. Station 3B of 2020 Semester 2 asks a 32-year-old attending an
immigration medical - no symptoms, no complaint - to "give me three
differential diagnoses for the patient's current presentation", because step 4
was mandatory. The candidate is left guessing what the examiner wants. Four
good questions beat six with two of them invented, and a report that supports
three questions is a three-question station.

TWO STEPS ARE NOT OPTIONAL WHEN THE MATERIAL IS THERE, and leaving them out
guts the station:
- If the images below include an ancillary test - an OCT, a visual field, an
  angiogram, an ultrasound, an MRI - one question MUST ask the candidate to
  read it. It is the only thing on the screen they can interpret, and a
  station that shows a macular OCT and a B-scan and asks about neither has
  wasted both.
- If the case has a diagnosis to reach, the candidate MUST be asked to reason
  towards it BEFORE step 5 gives it away. A station that goes from "examine
  both eyes" straight to "summarise your findings and give me your diagnosis"
  tests recognition and nothing else. Ask what else it could be, what would
  distinguish them, what the finding implies - that reasoning is what the
  examiners mark and what the candidate is practising.

Where a case genuinely carries more, add questions - a station with five aims
needs more than one with two:

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
   ONLY WHERE THERE IS SOMETHING TO EXAMINE. The list of images below is what
   the candidate will actually be shown. If it holds no view of the patient or
   the eye - no external photograph, no slit lamp, no fundus, no montage - and
   only ancillary tests such as an OCT, a B-scan or an MRI, then "Please
   examine the anterior segment and fundus" asks for an examination that
   cannot happen, and the marks on it cannot be earned by anyone. A live
   station did exactly that: six marks for describing an anterior segment and
   fundus, with a macular OCT and an ultrasound on screen and nothing else.
   In that case the first question is what the material supports - "Here is
   this patient's macular OCT and B-scan. Describe what they show." - and the
   describing marks belong to it.
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
     "What ancillary test would confirm that, and what would you expect?"
     "What would you do to confirm the site of the lesion?"
   Take it from the aims and the rubric - they name the test the examiners
   cared about. Where the candidate genuinely should propose the test
   unprompted, ask what they would do NEXT for this specific problem rather
   than naming the test for them.
3. READ THE ANCILLARY IMAGE. Having asked for it, they describe what it shows
   - correctly naming the sign, its extent, and what is absent. Ask it blind:
   "What does this show?" / "Describe the OCT."
   Ask this step whenever the case genuinely turns on an ancillary test or an
   investigation - the examiners' report is the guide, and a report that says
   candidates misread the MRI means the MRI was put in front of them. If the request below does
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
4. SUMMARISE AND DIFFERENTIATE, with a stated number - AND SAY WHAT THE
   DIFFERENTIAL IS FOR. "Give me three differential diagnoses for the
   patient's current presentation" is unanswerable when the patient has no
   presentation: station 3B of 2020 Semester 2 sends a 32-year-old in for an
   immigration medical with no complaint, shows a dislocated lens, and then
   marks the candidate for saying "UGH syndrome". Nothing in the question says
   what it is a differential OF, so it cannot be answered as intended.
   REFER to the subject, never ASSERT it. "You have described a coloboma with
   zonular insufficiency - what is your differential?" tells the candidate
   what they found, and on a live station it told them the wrong thing: they
   had described nothing yet, and the station was about a macular schisis.
   Anchor it to what the EXAMINER has already put in front of them, or to
   their own findings without naming those findings:
     "Summarise your findings and give me three differentials for the cause."
     "What is your differential for the appearance in these photographs?"
     "What is your differential for this patient's reduced vision?"
   Never begin "You have described...", "You have found...", "You noted...".
   The candidate says what they found; the examiner does not say it for them.
   Where the picture settles the diagnosis, a differential OF the diagnosis is
   not a question. Ask for the differential of its CAUSE, or for what
   threatens the eye next - and ask in those words rather than dressing it up
   as a differential.
   Whatever is marked here must not be marked again later. In that same
   station UGH syndrome was worth 0.5 as a "differential" at step 4 and 2.0 as
   a complication at step 5: the same sentence, paid for twice, and the
   candidate cannot tell which of the two the examiner wants first.
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

EVERY QUESTION MUST COME FROM THE REPORT, AND SAY SO.

Give each question a "drawn_from": the aim, the noted mistake or the rubric \
point it exists to test, quoted from the material above. This is not \
paperwork. A question written from the arc alone comes out as the same \
sentence every time - "Summarise your findings and give me three differential \
diagnoses for this patient's presentation", "How would you manage her if she \
were new to you and you had just made the diagnosis" - and a candidate who has \
sat four of these can answer the fifth without reading the case.

The report tells you what the examiners actually cared about. "Candidates were \
unable to interpret the 3-step test" is an instruction to ask about the 3-step \
test. "Not making the link between the radial keratotomy and the flatter \
central cornea" is an instruction to ask what the topography shows AND why. \
"Knowledge of screening regimes was poor" is an instruction to ask for the \
screening regime, by name, with its intervals.

So write the question the examiners would have asked about THIS case:
  not "What investigations would you order?"
      but "Which test would settle whether this is a fourth nerve palsy or a \
skew, and what would it show?"
  not "Summarise your findings and give three differentials."
      but "This lens is dislocated superotemporally. What does that direction \
tell you, and what would you look for systemically?"
  not "How would you manage her?"
      but "She is 4 and this eye is her only good one. Walk me through the \
first year."

Every aim must be reachable from some question, and every noted mistake must \
have a question that would expose it. A station that leaves one untested has \
left out the thing the examiners wrote the station for.

Return ONLY a JSON object:
{{
  "prompts": [
    {{"label": "A",
      "text": "the question as spoken",
      "drawn_from": "the aim, mistake or rubric point this question tests",
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
    # Whether there is anything to examine. A station whose only pictures are
    # ancillary tests cannot ask the candidate to examine an eye.
    has_view = any((f.modality or "") in VIEW_MODALITIES for f in figures)
    has_ancillary = any((f.modality or "") in ANCILLARY_MODALITIES for f in figures)
    # Everything the candidate can actually reach: what the examiner tells
    # them up front, and whatever the pictures show or say.
    available = " ".join(filter(None, [
        station.findings_given or "",
        *(f.caption or "" for f in figures),
        *(f.described_findings or "" for f in figures),
        *(f.wanted_description or "" for f in figures),
    ]))
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
        + ("" if has_view else
           "NONE of these is a view of the patient or the eye - they are ancillary "
           "tests. There is nothing to examine, so step 1 must ask the candidate to "
           "read what is on screen rather than to examine an eye they cannot see.\n")
        + "The first is the patient - what the candidate examines at step 1, not "
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
    problems = _arc_problems(
        prompts, has_image, needs_investigation, vocabulary, station.aims, has_view,
        has_ancillary, bool((station.diagnosis or '').strip()), station.diagnosis,
        station.findings_elicited, available,
    )
    if problems:
        retry_user = (
            user
            + "\n\nYour first attempt was rejected because:\n"
            + "\n".join(f"  - {p}" for p in problems)
            + "\n\nRewrite the whole sequence, fixing these."
        )
        retried, retry_warnings = _generate(client, retry_user, job_id)
        remaining = _arc_problems(
            retried, has_image, needs_investigation, vocabulary, station.aims, has_view,
            has_ancillary, bool((station.diagnosis or '').strip()), station.diagnosis,
            station.findings_elicited, available,
        )
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

# The two steps every station has, whatever the report contains: an
# instruction to begin on, and the reveal that stops later marks depending on
# earlier ones.
_ALWAYS = (1, 5)

# What each optional step needs the report to be about before it is demanded.
# Asking for a differential because the arc has a slot for one is how station
# 3B of 2020 Semester 2 came to ask a woman at an immigration medical for
# "three differential diagnoses for the patient's current presentation".
_STEP_EVIDENCE = {
    2: (
        "examin", "measur", "assess", "test", "gonioscop", "retinoscop",
        "refract", "technique", "perform", "check", "look",
    ),
    4: (
        "differential", "diagnos", "distinguish", "differentiat", "cause",
        "aetiolog", "etiolog", "recognis", "recogniz",
    ),
    6: ("progress", "follow", "monitor", "recur", "complicat", "if ", "later"),
    7: (
        "inherit", "genetic", "criteria", "classif", "mechanism", "pharmacolog",
        "association", "syndrome", "knowledge",
    ),
}


# Something the candidate READS rather than examines. A station that holds one
# and never asks about it wastes the only interpretable thing on the screen.
ANCILLARY_MODALITIES = {
    "oct", "visual_field", "angiogram", "radiology", "ultrasound", "pathology",
    "specular", "topography", "biometry",
}


def steps_the_case_supports(
    aims: list[str] | None,
    rubric_text: str = "",
    has_ancillary: bool = False,
    has_diagnosis: bool = False,
) -> set[int]:
    """Which arc steps this station's own report actually calls for.

    The arc was a mould: steps 1, 2, 4 and 5 were demanded of every station,
    plus 3 wherever an image existed and one of 6 or 7 always. A report that
    supports four questions was therefore made to yield six, and the two it
    could not support came out as filler the candidate has to guess at - a
    differential of nothing, a hypothetical about a case that does not evolve.

    A station is now allowed to be the shape its report is. Step 1 stays,
    because a station has to open with an instruction, and step 5 stays,
    because stating the diagnosis midway is what keeps the later marks earnable
    by a candidate who has not got there. Everything else has to be earned by
    the aims and the rubric saying it is that kind of case.
    """
    haystack = " ".join([*(aims or []), rubric_text]).lower()
    supported = set(_ALWAYS)
    for step, evidence in _STEP_EVIDENCE.items():
        if any(word in haystack for word in evidence):
            supported.add(step)

    # An OCT, a field or an MRI on the screen is the one thing on this station
    # the candidate can be asked to interpret. A live station showed a macular
    # OCT and a B-scan and asked about neither.
    if has_ancillary:
        supported.add(3)

    # And they must be asked to reason towards the diagnosis before it is given
    # to them. Making step 4 optional fixed a station that asked for a
    # differential of nothing, and broke the ones that should ask: another live
    # station went straight from "examine both eyes" to "summarise your
    # findings and give me your diagnosis", with no differential sought
    # anywhere. Where there is a diagnosis to reach, the reasoning is the
    # station. What step 4 may not do is fail to say what it is a differential
    # OF - that rule lives in `_generic_problems`.
    if has_diagnosis:
        supported.add(4)
    return supported

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


# What a rubric line says about being a rubric line, rather than about the
# case. Left in, "discuss ... risk" alone would match half the station.
_RUBRIC_VERBS = {
    "identify", "propose", "discuss", "describe", "comment", "recognise",
    "recognize", "state", "mention", "explain", "outline", "summarise",
    "summarize", "consider", "assess", "potential", "risk", "risks",
    "differential", "differentials", "diagnosis", "diagnoses", "complication",
    "complications", "management", "least", "candidate", "patient",
}


# A view of the patient or the eye - something a candidate can be asked to
# examine. An OCT is a test, not a view.
VIEW_MODALITIES = {"external", "slit_lamp", "fundus", "motility", "orthoptic", "photo"}

# Only where the examiner OPENS by telling the candidate what they found.
# "What is your differential for the occlusive vasculitis you have described?"
# is the recommended form - it refers to their answer rather than asserting it -
# and matching anywhere in the sentence rejected exactly that.
_ASSERTS_FINDINGS = re.compile(
    r"^\s*(?:so\s+|now\s+|ok(?:ay)?,?\s+)?you (?:have |had |'ve )?"
    r"(?:described|found|noted|identified|seen|observed)\b",
    re.IGNORECASE,
)

# "You mentioned auscultation. What would you be listening for?" - if they did
# not mention it, the question cannot be answered at all, and the candidate
# loses the mark for a conversation that never happened. Seen live on station
# 81. Unlike the assertion above this one may appear anywhere in the sentence.
_PRESUPPOSES_AN_ANSWER = re.compile(
    r"\byou (?:mentioned|said|suggested|indicated|referred to|talked about)\b"
    r"|\bas you (?:mentioned|said|noted)\b"
    r"|\byour (?:earlier|previous|last) (?:answer|response|reply)\b",
    re.IGNORECASE,
)

# Sentences the model reaches for when it is writing from the arc instead of
# from the report. Each is a real string that came back on many stations at
# once; a candidate who has sat four of them can answer the fifth blind.
#
# Only the content-free ones. "The diagnosis is X. How would you manage her if
# she were new to you?" appears on 140 stations and is NOT here: that wording
# is what the arc asks for and what a real examiner says, and rejecting it
# would reject nearly every station in the bank to fix nothing. What is here
# names no subject at all - "for this patient's presentation" on a patient who
# has no presentation is the station that sent a woman to an immigration
# medical and marked her differential.
_STOCK_SENTENCES = (
    "differential diagnoses for this patient's presentation",
    "differential diagnosis for this patient's presentation",
    "differentials for this patient's presentation",
)


def _presupposes_an_answer(prompts: list[dict[str, Any]]) -> list[str]:
    """A question that assumes the candidate volunteered something."""
    problems = []
    for prompt in prompts:
        match = _PRESUPPOSES_AN_ANSWER.search(str(prompt.get("text") or ""))
        if match:
            problems.append(
                f"question {prompt.get('label') or '?'} opens {match.group(0)!r}, "
                f"which assumes an answer the candidate may never have given - ask "
                f"the question on its own terms"
            )
    return problems


def _reveal_before_the_reading(prompts: list[dict[str, Any]]) -> list[str]:
    """The answer given, and then the candidate asked to work it out.

    Station 320 revealed "glaucoma secondary to Sturge Weber" at question C and
    asked the candidate to talk through the OCT and RNFL at question D, for 4.5
    marks they could not fail to earn. Station 110 revealed the diagnosis and
    then asked them to summarise and give it.

    Only reading and concluding, not the questions that legitimately follow a
    reveal: "what would you expect the B-scan to show" and "what ancillary
    tests would confirm it" are asked AFTER the diagnosis on purpose.
    """
    problems: list[str] = []
    revealed = False
    for prompt in prompts:
        step = int(as_float(prompt.get("step"), 0.0) or 0)
        if step == 5:
            revealed = True
            continue
        if not revealed or step not in (3, 4):
            continue
        text = str(prompt.get("text") or "")
        # A hypothetical about what a test WOULD show stands on its own.
        if re.search(r"\bwould you expect\b|\bif\b", text, re.IGNORECASE):
            continue
        problems.append(
            f"question {prompt.get('label') or '?'} asks the candidate to "
            f"{'read a test' if step == 3 else 'reach the diagnosis'} after the "
            f"diagnosis has already been given - it must come before the reveal"
        )
    return problems


def _written_from_the_arc_not_the_report(
    prompts: list[dict[str, Any]],
) -> list[str]:
    """Questions that could belong to any station in the bank."""
    problems: list[str] = []
    for prompt in prompts:
        label = prompt.get("label") or "?"
        text = str(prompt.get("text") or "").lower()
        for stock in _STOCK_SENTENCES:
            if stock in text:
                problems.append(
                    f"question {label} uses the stock sentence {stock!r}, which "
                    f"fits every station and tests none - write what the "
                    f"examiners' report says this station was about"
                )
                break
    # Rejecting a station because one question forgot to cite itself would
    # throw away five good questions to punish a missing string - which is how
    # a whole rebuild was lost to this gate before. It bites when the
    # instruction was ignored wholesale, not when it was imperfectly followed.
    cited = sum(1 for p in prompts if str(p.get("drawn_from") or "").strip())
    if prompts and cited * 2 < len(prompts):
        problems.append(
            f"only {cited} of {len(prompts)} questions say which aim, mistake or "
            f"rubric point they came from - write the questions from the "
            f"examiners' report and name what each one tests in \"drawn_from\""
        )
    return problems


def _tells_the_candidate_what_they_found(prompts: list[dict[str, Any]]) -> list[str]:
    """A question that says what the candidate found, before they have said it.

    Written while fixing something else: "a differential must say what it is a
    differential of" was obeyed by asserting the finding. A live station then
    opened question 2 with "You have described a coloboma with zonular
    insufficiency" - to a candidate who had described nothing, on a station
    whose opening screen said macular schisis. Both halves wrong, and the
    second half is a leak.
    """
    problems = []
    for prompt in prompts:
        match = _ASSERTS_FINDINGS.search(str(prompt.get("text") or ""))
        if match:
            problems.append(
                f"question {prompt.get('label') or '?'} says {match.group(0)!r} - the "
                f"candidate says what they found, not the examiner; refer to it "
                f"without asserting it"
            )
    return problems


def _examines_what_cannot_be_seen(
    prompts: list[dict[str, Any]], has_view: bool
) -> list[str]:
    """Marks for examining an eye the candidate is never shown.

    The standing instruction carries every mark for describing the signs. Where
    the station's only images are ancillary tests, there is no eye to examine
    and those marks cannot be earned by anyone: one live station put six of its
    twenty on "examine the anterior segment and fundus" with a macular OCT and
    a B-scan on screen and nothing else.
    """
    if has_view:
        return []
    for prompt in prompts:
        if prompt.get("step") != 1:
            continue
        if re.search(r"\bexamin\w*\b", str(prompt.get("text") or ""), re.IGNORECASE):
            marks = sum(float(p.get("marks") or 0) for p in prompt.get("rubric") or [])
            return [
                f"question {prompt.get('label') or '?'} asks the candidate to examine, "
                f"but the station shows no view of the patient or the eye - only "
                f"ancillary tests, so its {marks:g} mark(s) cannot be earned; ask them "
                f"to read what is actually on screen"
            ]
    return []


def _diagnosis_named_before_the_reveal(
    prompts: list[dict[str, Any]],
    diagnosis: str | None,
    elicited: str | None = None,
) -> list[str]:
    """A question that names the diagnosis before step 5 hands it over.

    The prompt has always said not to, and the model does it anyway. A live
    station asked the candidate to describe a macular OCT, and then at question
    2: "how would you manage the diabetic macular oedema pre-operatively?" -
    followed at question 3 by "give me three differential diagnoses for the
    patient's reduced vision". The differential was already answered two
    questions earlier, by the examiner.

    Step 5 states it deliberately, and everything after may use the name.
    """
    if not (diagnosis or "").strip():
        return []
    # Imported here: verify -> sittability -> prompts closes the circle.
    from app.services.osce.station_images.verify import leaked_term

    station = SimpleNamespace(
        diagnosis=diagnosis, findings_elicited=elicited, findings=elicited
    )
    problems = []
    for prompt in prompts:
        step = prompt.get("step")
        if not isinstance(step, int) or step >= 5:
            continue
        # The LENIENT guard, not the strict one. A recorded diagnosis carries
        # the signs with it - "CPEO with bilateral ptosis", "multifocal
        # choroiditis with a choroidal neovascular membrane" - so the strict
        # rule made "ptosis" and "multifocal" forbidden words and rejected
        # "what further measurements would you perform to assess the ptosis?",
        # which is the examiner naming a sign the candidate has just described.
        # `leaked_term` forgives a word the station's own findings use, which
        # is exactly the distinction wanted here: the sign is fair, the label
        # is not.
        named = leaked_term(
            str(prompt.get("text") or ""), station, conclusions=False
        )
        if named:
            problems.append(
                f"question {prompt.get('label') or '?'} names {named!r} before step 5 "
                f"gives the diagnosis - every question after it is answered in advance"
            )
    return problems


def _presupposes_an_unreachable_sign(
    prompts: list[dict[str, Any]],
    elicited: str | None,
    available: str,
    has_view: bool = False,
) -> list[str]:
    """A question that refers to a sign the candidate was never given a way to find.

    "How would you manage him, specifically addressing THE ZONULAR WEAKNESS?"
    on a station whose only picture is a macular OCT. Zonular weakness is a
    slit lamp finding; it is in the paper's elicited findings, it is on no
    image the candidate is shown, and no words state it. The question assumes
    they found it, and marks them as though they had.

    Deliberately narrow, because being broad here has cost a day:
      * only the definite reference - "the zonular weakness", "this cataract" -
        since that is what presupposes rather than asks;
      * only signs the paper records as ELICITED, so nothing given up front
        counts;
      * only where the sign appears nowhere the candidate can reach it: not in
        the background, not in a caption, not in any stated findings;
      * and only before the reveal, since afterwards the examiner has handed
        the case over and may refer to any of it.
    An examiner naming a sign the candidate has just described is proper, and
    that sign will be visible in an image or stated in words, so it is not
    caught here.
    """
    if not (elicited or "").strip() or has_view:
        # There is a patient to examine, so the elicited signs are exactly what
        # the candidate is meant to go and find - "what measurements would you
        # perform to assess the ptosis?" follows them describing the ptosis.
        # Without this the check fired on every station with a photograph, and
        # the fault it was written for is the opposite case: a station whose
        # only picture is an OCT, asking about zonular weakness.
        return []
    reachable = _content_words(available)
    unreachable = _content_words(elicited) - reachable - _GENERIC_WORDS
    if not unreachable:
        return []

    problems = []
    for prompt in prompts:
        step = prompt.get("step")
        if not isinstance(step, int) or step >= 5:
            continue
        text = str(prompt.get("text") or "")
        for word in sorted(unreachable):
            pattern = (
                r"\b(?:the|this|that|his|her|their)\s+"
                rf"(?:\w+\s+){{0,2}}{re.escape(word)}\b"
            )
            found = re.search(pattern, text, re.IGNORECASE)
            if found:
                # The phrase, not the word that tripped it: "the zonular
                # weakness" tells you what to fix, "weakness" does not.
                problems.append(
                    f"question {prompt.get('label') or '?'} refers to "
                    f"{found.group(0)!r} as though the candidate had found it, but "
                    f"nothing they are shown or told contains it"
                )
                break
    return problems


def _points_marked_twice(
    prompts: list[dict[str, Any]], vocabulary: set[str] | None = None
) -> list[str]:
    """The same thing paid for at two questions.

    Station 3B of 2020 Semester 2 marks "UGH syndrome" 0.5 as a differential at
    step 4 and 2.0 as a complication at step 5. The candidate cannot tell which
    question wants it, saying it at the first loses the second, and twenty
    marks are supposed to cover twenty different things.

    Matched on the distinctive words a point is about, so "Propose UGH syndrome
    as a differential" and "Discuss the risk of Uveitis-Glaucoma-Hyphema (UGH)
    syndrome as a potential complication" are recognised as one point.

    Two things are stripped before comparing. Rubric verbs, or every "discuss"
    would collide. And the station's OWN vocabulary, because every point on a
    paediatric cataract station says "paediatric cataract": without that,
    "Discusses vision rehabilitation strategies", "Discusses considerations in
    management" and "Discusses regular risks of surgery" all read as one point,
    which is three false accusations on a station whose rubric is fine. What is
    left is what makes a point *that* point - and "ugh syndrome" survives it,
    because the diagnosis there is "Dislocated IOL".
    """
    problems: list[str] = []
    seen: dict[frozenset[str], str] = {}
    common = vocabulary or set()
    for prompt in prompts:
        label = prompt.get("label") or "?"
        for point in prompt.get("rubric") or []:
            text = str(point.get("text") or "")
            # Acronyms carry the whole meaning of a point and `_content_words`
            # cannot see them: "UGH" is three letters and its floor is four.
            # Without this, "Propose UGH syndrome as a differential" reduces to
            # {syndrome} and matches nothing.
            words = (_content_words(text) - _RUBRIC_VERBS - common) | {
                a.lower() for a in re.findall(r"\b[A-Z]{2,6}\b", text)
                if a.lower() not in common
            }
            if len(words) < 2:
                continue
            key = frozenset(words)
            for earlier, where in seen.items():
                # Two points of the SAME question are two things that question
                # asks for, not one thing paid for twice. Comparing a question
                # with itself reported "question A marks X, which question A
                # already marks".
                if where == label:
                    continue
                shared = earlier & key
                # Two-thirds of the smaller point, so a longer restatement of
                # the same thing still counts as the same thing.
                if len(shared) >= max(2, int(min(len(earlier), len(key)) * 0.67)):
                    problems.append(
                        f"question {label} marks {str(point.get('text'))[:50]!r}, which "
                        f"question {where} already marks - one thing, paid for twice"
                    )
                    break
            else:
                seen[key] = label
    return problems


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

    # Step 1 is formulaic in the real exam too: "Please examine the posterior
    # segment of both eyes" is exactly what is said, and naming a structure
    # there would tell the candidate where to look. Every other step, step 4
    # included, has to be about this case.
    #
    # Step 4 was exempt here, on the same reasoning, and it is the reason
    # station 3B of 2020 Semester 2 asks for "three differential diagnoses for
    # the patient's current presentation" of a woman attending an immigration
    # medical with no complaint. A differential has to say what it is a
    # differential OF, and the only way to say that is in this case's words.
    for prompt in prompts:
        # Step 1 names a region and an eye and nothing else, on purpose. Step 3
        # is asked BLIND on purpose - "What does this show?" is what the arc
        # above tells the model to write, and then this called it generic and
        # sent the station back. A rule that rejects the thing another rule
        # demands costs a retry and teaches the model nothing.
        if prompt.get("step") in (1, 3) or not prompt.get("step"):
            continue
        # "Summarise your findings and give me three differentials for the
        # cause" says perfectly well what it is about: the candidate's own
        # findings. It shares no disease word with the case, so a
        # vocabulary-only test called it unanchored - forty-nine times.
        text = str(prompt.get("text") or "")
        anchored = re.search(
            r"\byour findings\b|\bthese findings\b|\bthe findings\b|"
            r"\bwhat you (?:have )?(?:just )?(?:described|found|seen)\b",
            text, re.IGNORECASE,
        ) and not re.search(
            # Except this one, which is the phrase that started all of it:
            # "three differential diagnoses for the patient's CURRENT
            # PRESENTATION" of a woman at an immigration medical who has not
            # presented with anything. Naming the findings and then hanging the
            # differential on a presentation that does not exist is still
            # leaving the candidate to guess.
            r"\b(?:current|this|the)\s+presentation\b", text, re.IGNORECASE,
        )
        if not (_content_words(prompt["text"]) & vocabulary) and not anchored:
            if prompt.get("step") == 4:
                problems.append(
                    f"question {prompt['label']} ({prompt['text'][:60]!r}...) asks for a "
                    "differential without saying what it is a differential of - name the "
                    "finding the candidate has been shown or asked to describe"
                )
            else:
                problems.append(
                    f"question {prompt['label']} ({prompt['text'][:60]!r}...) says nothing "
                    "specific to this case - it would fit any station; ask what this case's "
                    "aims and rubric actually turn on"
                )

    asked = _content_words(" ".join(p["text"] for p in prompts))
    # Matched on a shared opening, not the whole word. "To examine the eyelid
    # in a systematic manner" against "Please examine the eyelids of both eyes"
    # was reported as an aim never asked about, on a plural - eighty-four of
    # one count were that kind of miss.
    asked_roots = {w[:5] for w in asked}
    for aim in aims or []:
        wanted = _content_words(aim) & vocabulary
        # An aim like "Describe findings." carries nothing to check against.
        if len(wanted) < 2:
            continue
        if not any(w[:5] in asked_roots for w in wanted):
            problems.append(
                f"the aim {aim[:70]!r} is never asked about; it is what the station "
                "exists to test, so it needs a question of its own"
            )
    return problems


# A question that hands something over: "These are the corneal topography and
# biometry for both eyes." The candidate is being told to look at a thing, so
# the thing has to exist.
PRESENTS_INVESTIGATION_RE = re.compile(
    r"\b(?:this is|these are|here is|here are|shown (?:here|below) (?:is|are))\b"
    r"[^.?!]{0,80}?\b(?:OCT\b|OCT-A|MRI|CT\b|B-?scan|A-?scan|ultrasound|UBM|"
    r"angiogra\w+|topograph\w+|tomograph\w+|biometry|specular|autofluorescence|"
    r"FAF|ERG|visual\s+field|perimetry|Hess\s+chart|photograph|image|scan|"
    r"printout|x-?ray|radiograph)",
    re.IGNORECASE,
)


def _unshowable_questions(prompts: list[dict[str, Any]]) -> list[str]:
    """Questions that present an investigation without ever asking for one.

    The station's own instructions already forbid this - "never present a
    result you have not either been given or asked for" - but nothing checked,
    and a candidate reached "These are the corneal topography and biometry for
    both eyes. What do they show?" with an empty screen.

    This is the invariant the pipeline never had: a question the candidate is
    asked must be answerable from what they can see. It is enforced here, at
    the point the question is written, because that is the only place where
    fixing it costs nothing - by the time the station is sat, the wording is
    baked in and the marks have been apportioned to it.
    """
    problems: list[str] = []
    for prompt in prompts:
        text = str(prompt.get("text") or "")
        if not PRESENTS_INVESTIGATION_RE.search(text):
            continue
        if str(prompt.get("image_wanted") or "").strip():
            continue
        problems.append(
            f"question {prompt.get('label') or '?'} presents an investigation "
            f"({text[:48]!r}) but gives no image_wanted, so the candidate would "
            f"be asked to read a blank screen"
        )
    return problems


def _unmarked_questions(prompts: list[dict[str, Any]]) -> list[str]:
    """Questions the candidate answers for nothing.

    The builder is told the marks must total 20 and that is checked, but
    nothing said every question must carry some - so the model concentrates
    them and leaves the rest at zero. 147 questions across 98 stations ended up
    worth nothing, one station with three of its six, and the marker replies
    "This question carries no marks" to an answer that took a minute of a
    nine-minute station to give.

    A question worth nothing is not a question. Either it earns marks or it
    should not be asked.

    The same fault hides one level down, and this only caught the top one. A
    question carrying four rubric lines and 1.5 marks between them pays for
    three of them: half a mark is the finest award an examiner can make, so the
    fourth is worth nothing and cannot be earned however well it is answered.
    Eleven stations were built that way. A question must be able to pay for
    every line it holds - half a mark each, at least.
    """
    problems = []
    for index, prompt in enumerate(prompts):
        label = prompt.get("label") or index
        rubric = prompt.get("rubric") or []
        marks = sum(pt.get("marks", 0) or 0 for pt in rubric)
        if marks <= 0:
            problems.append(
                f"question {label} carries no marks; every question "
                f"must be worth at least 1 of the 20"
            )
            continue
        dead = [
            point.get("text") or f"line {i + 1}"
            for i, point in enumerate(rubric)
            if (point.get("marks") or 0) <= 0
        ]
        if dead:
            problems.append(
                f"question {label} has {len(dead)} rubric line(s) worth nothing "
                f"({str(dead[0])[:48]!r}); a line nobody can be awarded is not a "
                f"marking point"
            )
        elif marks < 0.5 * len(rubric):
            problems.append(
                f"question {label} is worth {marks:g} but carries {len(rubric)} "
                f"lines; half a mark each is the least they can be paid, so it "
                f"needs at least {0.5 * len(rubric):g} or fewer lines"
            )
    return problems


def _arc_problems(
    prompts: list[dict[str, Any]],
    has_image: bool = True,
    needs_investigation: bool = False,
    vocabulary: set[str] | None = None,
    aims: list[str] | None = None,
    has_view: bool = True,
    has_ancillary: bool = False,
    has_diagnosis: bool = False,
    diagnosis: str | None = None,
    elicited: str | None = None,
    available: str = "",
) -> list[str]:
    """Check the sequence against the arc. Empty means it is a real station."""
    problems: list[str] = []
    steps = [p.get("step") for p in prompts]
    problems.extend(_generic_problems(prompts, vocabulary, aims))
    problems.extend(_unshowable_questions(prompts))
    problems.extend(_unmarked_questions(prompts))
    problems.extend(_points_marked_twice(prompts, vocabulary))
    problems.extend(_tells_the_candidate_what_they_found(prompts))
    problems.extend(_presupposes_an_answer(prompts))
    problems.extend(_reveal_before_the_reading(prompts))
    problems.extend(_written_from_the_arc_not_the_report(prompts))
    problems.extend(_examines_what_cannot_be_seen(prompts, has_view))
    problems.extend(
        _diagnosis_named_before_the_reveal(prompts, diagnosis, elicited)
    )
    problems.extend(
        _presupposes_an_unreachable_sign(prompts, elicited, available, has_view)
    )

    # What this particular report supports, not a fixed shape. Step 3 reads an
    # ancillary image: the station need not already have one - a question that
    # asks for it says what it needs and the image is sourced - so it is
    # required when there is one to read, or when the rubric marks the
    # candidate on reading one.
    rubric_text = " ".join(
        str(point.get("text") or "")
        for prompt in prompts
        for point in prompt.get("rubric") or []
    )
    required = steps_the_case_supports(
        aims, rubric_text, has_ancillary=has_ancillary, has_diagnosis=has_diagnosis
    )
    if needs_investigation:
        required.add(3)
    for step in sorted(required):
        if steps.count(step) != 1:
            problems.append(
                f"arc step {step} appears {steps.count(step)} times; it must appear exactly once"
            )
    # A step the report says nothing about may be absent, but must not be
    # present twice if it is there at all.
    for step in sorted({s for s in steps if isinstance(s, int)} - required):
        if steps.count(step) > 1:
            problems.append(
                f"arc step {step} appears {steps.count(step)} times; it must appear at most once"
            )

    # The reveal must not land before the candidate has been asked to reason -
    # but only where this case calls for a differential at all. Demanding one
    # before every reveal, while step 4 is optional, is two of my own rules
    # fighting: nineteen stations were told both that they must not have a
    # differential and that they must have one before question D.
    if 4 in required:
        index = needs_differential_first(prompts)
        if index is not None:
            problems.append(
                f"question {prompts[index].get('label') or index} states the diagnosis, but "
                f"no earlier question asks for a differential - the candidate is told the "
                f"answer before being asked to reason towards it"
            )

    # Arc order, with steps 3 and 4 free to swap. "What is your differential,
    # and what would you order?" followed by "here is the MRI, what does it
    # show?" is how a real station runs, and insisting the image always comes
    # first rejected it.
    ordered = [3 if s == 4 else s for s in steps if isinstance(s, int)]
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
                # The aim, noted mistake or rubric point this question exists
                # to test, in the report's own words. Kept so a station can be
                # read back against the examiners' account of it, and checked
                # so a question written from the arc alone does not ship.
                "drawn_from": str(item.get("drawn_from") or "").strip() or None,
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

    # Marks must total 20, for the same reason they must in a written paper,
    # and they must be whole - an examiner cannot award 3.06 of a mark.
    all_points = [pt for p in prompts for pt in p["rubric"]]
    total_marks = sum(pt["marks"] for pt in all_points)
    if total_marks > 0 and abs(total_marks - STATION_MARKS) > 0.01:
        warnings.append(
            f"Rubric totalled {total_marks:g} marks; rescaled to {STATION_MARKS}."
        )
        if not rescale_marks_to_awardable(all_points, STATION_MARKS):
            # More rubric lines than marks available: fall back to proportional
            # fractions rather than dropping lines from the key.
            warnings.append(
                f"{len(all_points)} rubric lines share {STATION_MARKS} marks, so "
                f"marks could not be kept whole."
            )
            factor = STATION_MARKS / total_marks
            for point in all_points:
                # Never below half a mark. Scaling proportionally rounds a
                # small line to 0.0 and writes a marking point nobody can ever
                # be awarded - which is the fault the rescale above exists to
                # avoid, reappearing in the path taken when it gives up.
                point["marks"] = max(0.5, round(point["marks"] * factor, 2))
            absorb_mark_drift(all_points, STATION_MARKS)
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
    finished = index + 1 >= len(station_ids)
    if finished:
        _rebind_figures(ctx, station_ids)
        _rewrite_model_answers(ctx, station_ids)
    return finished


def _rewrite_model_answers(ctx: JobContext, station_ids: list[int]) -> None:
    """The answers belong to the marking points, and the points were just rewritten.

    Same orphaning as the figure bindings, one field over: a model answer is
    stored on its rubric point, and the review joins it back by matching the
    point's text. Rebuilding prompts writes new points, so the answers stop
    matching and the review shows a mark and a comment with no answer beside
    it - which is the part worth reading.
    """
    from app.services.jobs.runner import create_job
    from app.services.osce.model_answers import JOB_MODEL_ANSWERS

    job = create_job(
        ctx.db,
        JOB_MODEL_ANSWERS,
        payload={"station_ids": sorted(station_ids)},
        created_by_id=ctx.job.created_by_id,
        total_steps=len(station_ids),
        message=f"Rewriting model answers for {len(station_ids)} station(s)",
    )
    logger.info("Queued model answer job %s after a prompt rebuild", job.id)


def _rebind_figures(ctx: JobContext, station_ids: list[int]) -> None:
    """Re-attach the paper's figures to the questions that were just rewritten.

    A prompt carries the id of the figure it shows. Rebuilding prompts writes
    new ones, so every binding the figure check had made is gone - and the
    station is left asking "what would this ancillary test show?" beside a
    blank screen, while the OCT the report printed sits unclaimed two rows
    away. Seven stations of 2019 Semester 2 looked like missing images and were
    nothing of the kind.

    Free: the binder makes no model calls. It compares the modality a question
    named against the modality a figure was recorded as, and refuses anything
    short of an exact match.
    """
    from app.services.jobs.runner import create_job
    from app.services.osce.station_images.constants import JOB_BIND_STATION_FIGURES

    job = create_job(
        ctx.db,
        JOB_BIND_STATION_FIGURES,
        payload={"station_ids": sorted(station_ids)},
        created_by_id=ctx.job.created_by_id,
        total_steps=len(station_ids),
        message=f"Re-attaching figures for {len(station_ids)} station(s)",
    )
    logger.info("Queued figure re-bind job %s after a prompt rebuild", job.id)


def stations_needing_prompts(db: Session) -> list[int]:
    return list(
        db.execute(
            select(OsceStation.id)
            .where(OsceStation.prompts_status.in_(["none", "failed"]))
            .order_by(OsceStation.id)
        ).scalars().all()
    )



# --- The reveal must follow a differential ---------------------------------
REVEALS_DIAGNOSIS_RE = re.compile(r"\bthe diagnosis (?:is|here is)\b", re.I)
ASKS_DIFFERENTIAL_RE = re.compile(r"\bdifferential", re.I)
# "give me your leading diagnosis", "and give me a diagnosis", and the six
# other wordings the bank actually uses.
_ASKS_ONE_DIAGNOSIS_RE = re.compile(
    r",?\s*and\s+(?:give\s+me|tell\s+me)\s+"
    r"(?:a|your|the|my)?\s*"
    r"(?:leading|likely|most likely|working|presumed|final|unifying)?\s*"
    r"diagnosis\b",
    re.I,
)

_DIFFERENTIAL_CLAUSE = ", and give me your differential diagnoses and which you favour"
_DIFFERENTIAL_APPEND = " And what are your differential diagnoses?"


def needs_differential_first(prompts: list[dict[str, Any]]) -> int | None:
    """Index of a question that states the diagnosis with none asked for first.

    Stating the diagnosis mid-station is how the real examiners stop later
    marks depending on earlier ones - Lisa Cooke is told she has the 11778
    mutation and then asked what it means. But in the handouts the reveal
    always lands *after* the candidate has been asked to reason: "What are
    your differential diagnoses so far?" comes first, grouped by hereditary,
    compressive, inflammatory and infective.

    Where the bank asks only for "your leading diagnosis" and then announces
    the answer, the candidate names one thing, is told the answer, and is never
    asked to think across possibilities at all. Fifty-six stations do that.
    """
    texts = [str(p.get("text") or "") for p in prompts]
    index = next((i for i, t in enumerate(texts) if REVEALS_DIAGNOSIS_RE.search(t)), None)
    if index is None:
        return None
    if any(ASKS_DIFFERENTIAL_RE.search(t) for t in texts[:index]):
        return None
    return index


def ask_for_differentials(text: str) -> str:
    """Turn a question that asks for one diagnosis into one that asks for several.

    Deterministic, because the bank says it eight ways and all of them are the
    same sentence. "which you favour" is kept deliberately: the rubric for
    these questions awards a mark for naming the diagnosis, and a rewrite that
    dropped it would leave a marking key asking for something the question no
    longer requests - the exact fault this session has spent its day undoing.
    """
    if ASKS_DIFFERENTIAL_RE.search(text):
        return text
    replaced, count = _ASKS_ONE_DIAGNOSIS_RE.subn(_DIFFERENTIAL_CLAUSE, text, count=1)
    if count:
        return replaced
    # No diagnosis clause to convert, so the differential is added instead.
    return text.rstrip() + _DIFFERENTIAL_APPEND
