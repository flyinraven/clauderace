"""The gate: does this photograph actually show the station's findings?

An image that contradicts the rubric is worse than none, because the candidate
is then marked down for correctly describing what they can see. Everything here
exists to refuse an image, not to find one.
"""

from __future__ import annotations

import re
from typing import Any
from sqlalchemy.orm import Session
from app.models import OsceStation
from app.services.ai import AIClient, ImagePart, TextPart
from app.services.imagesearch.relevance import expected_modalities


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
optic nerve nerves chiasm corneal scleral retinal macular choroid choroidal
vitreous lenticular conjunctival ciliary limbal foveal peripapillary
poorly well fully partially freely easily readily barely equally briskly
sluggishly incompletely symmetrically evenly clearly visibly obviously
appears appear appeared seems looks looking towards along across between
larger smaller equal unequal bigger reacts react reacting reaction reactive
responds response constricts constrict constricting dilates dilate dilated
brisk sluggish target fixation blink blinks closes closed opens open
greater lesser difference size shape position movement excursion range
status post previous history also known stable untreated residual
only signs surgery despite childhood muscles angle peripheral
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


def _significant(text: str | None) -> list[str]:
    """The words of a phrase that carry a clinical claim, in order."""
    innocuous = _DIAGNOSIS_STOPWORDS | _GENERIC_WORDS
    return [
        w
        for w in re.findall(r"[a-z][a-z'\-]{3,}", (text or "").lower())
        if w not in innocuous
    ]


def _adjacent_pairs(words: list[str]) -> set[frozenset[str]]:
    """Neighbouring word pairs, unordered.

    Unordered because "overaction of the inferior oblique" and "inferior
    oblique overaction" are the same phrase, and a rule that caught only one of
    them catches nothing - the model writes whichever reads better.
    """
    return {frozenset(pair) for pair in zip(words, words[1:]) if len(set(pair)) == 2}


def leaked_term(text: str, station: OsceStation) -> str | None:
    """What this description gives away, or None if it only reports signs.

    Deterministic rather than another model call: it has to be reliable, it
    runs on every description, and it has to be free.

    Three ways to give the game away.

    Characterising the sign is the subtlest: "congruous, with macular sparing"
    names no diagnosis at all and is still the whole answer.

    Naming the condition is the obvious one - but a word check alone could not
    tell the name from the sign, and refused almost every description ever
    written. Of 38 figures put through the pass, 37 had good descriptions
    thrown away: a monocular elevation deficiency may not say "elevation", a
    glaucomatous disc may not say "cupping", and station 40 could not say
    "stable" because its diagnosis says "status post DMEK". The station was
    left with no image and no words, which is the one thing a candidate cannot
    work with.

    What separates the name from the sign is the station's own recorded
    findings. Those are what the examiners printed, and they are the signs the
    candidate is meant to have described to them: a word they use is a word
    this may use. A word that appears only in the diagnosis - "myasthenia",
    "Fuchs", "keratoconus" - is the label, and the label is the answer.

    The label is rarely one word, though. "Partially accommodative esotropia"
    is a station whose findings say all three, so every word of it is fair -
    and stating them together is still handing over the diagnosis. So any
    neighbouring pair of the diagnosis's own words is refused however well
    grounded each half is, which is what stops the findings being quoted back
    as a name.
    """
    conclusion = _CONCLUSION_RE.search(text)
    if conclusion:
        return conclusion.group(0)

    # Only the diagnosis, not the case summary. The summary is prose full of
    # ordinary clinical vocabulary - checking it rejected "there is a defect in
    # the left half of each field" because the summary happened to say "field".
    diagnosis = _significant(station.diagnosis)
    if not diagnosis:
        return None

    spoken = _significant(text)
    named = _adjacent_pairs(diagnosis) & _adjacent_pairs(spoken)
    if named:
        return " ".join(sorted(next(iter(named))))

    # Anatomy is not a giveaway. "Adie's pupil" made "pupil" a forbidden word,
    # and a dilated pupil with light-near dissociation cannot be described
    # without it. The same trap sits under every diagnosis named after a
    # structure: optic disc drusen, band keratopathy, macular hole. What gives
    # the answer away is the distinctive part, "Adie".
    allowed = set(
        _significant(getattr(station, "findings_elicited", None))
        + _significant(getattr(station, "findings", None))
    )
    for word in diagnosis:
        if _grounded(word, allowed):
            continue
        if re.search(rf"\b{re.escape(word)}\w*", text, re.IGNORECASE):
            return word
    return None


def names_the_diagnosis(
    text: str, station: OsceStation, conclusions: bool = True
) -> str | None:
    """The strict reading, for words written under a picture the candidate opens on.

    `leaked_term` forgives any word the station's own findings use, because it
    has to: it guards the last resort of the image protocol, where the words
    ARE the station, and a rule that strikes them leaves the candidate with
    nothing. Of 38 figures put through a stricter version, 37 lost good
    descriptions.

    None of that reasoning holds where there is a photograph. Striking the
    words there costs a caption and keeps the picture, so the balance flips and
    the station can afford the strict rule: a word of the diagnosis is refused
    however well the findings ground it.

    That matters most on the opening screen, which is what the candidate meets
    before they have said anything. Station 3B of 2020 Semester 2 opened with
    "A dislocated PMMA IOL is present" against a diagnosis of "Dislocated IOL" -
    grounded, accurate, and the entire answer. A figure a question owns is
    judged by `leaked_term` instead: by then the examiner has asked, and naming
    what a pathology slide shows is the point of showing it.
    """
    # A caption saying "consistent with" is drawing the conclusion for the
    # candidate. A QUESTION saying "give me three differential diagnoses" is
    # asking them to draw it, so callers checking question text turn this off.
    if conclusions:
        conclusion = _CONCLUSION_RE.search(text)
        if conclusion:
            return conclusion.group(0)

    # Strict about grounding, not about anatomy. "Adie's pupil" must not make
    # "pupil" a forbidden word, and a station about a lateral rectus cannot be
    # described without saying "lateral" - the same trap `leaked_term` names,
    # and it fires harder here because nothing else forgives it. What is
    # refused is the distinctive part: "Adie", "dislocated", "pseudoexfoliation".
    from app.services.osce.findings import _STEM_VOCABULARY

    # Words a diagnosis field carries because it is a diagnosis field, not
    # because they are the answer. Recorded diagnoses read "The diagnosis is
    # X", so "diagnosis" became a forbidden word and every question asking for
    # a differential diagnosis was refused - sixteen stations of one run.
    # Anatomy goes the same way: "anterior" is not the answer to anything.
    boilerplate = {
        "diagnosis", "diagnoses", "differential", "differentials", "presumed",
        "probable", "likely", "presentation", "patient", "station",
        "bilateral", "unilateral", "anterior", "posterior", "segment",
    }
    innocuous = _GENERIC_WORDS | _STEM_VOCABULARY | boilerplate
    spoken = set(_significant(text))
    candidates = {
        w for w in set(_significant(station.diagnosis)) - innocuous
        # "73-year-old" is the patient, not the answer, and several diagnoses
        # are recorded with the demographic attached.
        if not any(ch.isdigit() for ch in w)
    }
    for word in candidates:
        # Same root matching the rest of the module uses, so "dislocation"
        # answers for "dislocated" and "melanoma" for "melanomas".
        root = word[:5]
        if any(other.startswith(root) for other in spoken):
            return word
    # "TB-associated panuveitis" and "LHON" are two letters and four, so the
    # word check above cannot see them.
    acronym_re = re.compile(r"\b[A-Z]{2,6}\b")
    acronyms = {a.lower() for a in acronym_re.findall(station.diagnosis or "")}
    said = {a.lower() for a in acronym_re.findall(text)}
    overlap = acronyms & said
    if overlap:
        return sorted(overlap)[0]
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


def verbatim_findings_floor(
    station: OsceStation, wanted: str | None
) -> tuple[str | None, str | None]:
    """The station's printed findings, where they can honestly stand in.

    When no image can be found, `describe_findings` is asked to state the signs
    in words. It is given the station's recorded findings and the view in
    question, and told to say nothing the findings do not contain - so it is
    already the thing that judges whether they describe that view. When it
    declines, this is the floor beneath it.

    The floor is only for a figure that named no particular view: that figure is
    the station's own examination, and the printed findings are exactly what the
    examiner would state for it. A named view - a gaze montage, a CT angiogram,
    an OCT - gets words from the model or none at all.

    Quoting them anyway is how station 9A came to offer "Fundus examination is
    normal" for a nine-positions-of-gaze montage and for a CT angiogram of the
    circle of Willis. Two attempts to separate those cases by rule failed:
    modality class let the montage through, and word overlap refused correct
    pairings, because "slit lamp photograph of the anterior segment" and
    "stromal opacity" share no word while describing the same thing. Relevance
    is a judgement, the model is already making it, and a station with no words
    is visible in the admin page and on the station itself - while wrong words
    read as fact and are marked against.
    """
    if (wanted or "").strip():
        return None, None
    recorded = (station.findings_elicited or station.findings or "").strip()
    if not recorded or leaked_term(recorded, station):
        return None, None
    return recorded, "stated verbatim from the station's recorded findings"


BLIND_SYSTEM = """\
You are describing one clinical image. You are given NO clinical context, no
diagnosis and no expected findings, and there is nothing to agree with.
Describe only what is visible.

Report the laterality from the image alone. "one_eye" when a single eye is
shown, or when several panels all show the same one eye. "both_eyes" when two
different eyes appear. "unclear" when you cannot tell.

Then say WHICH in `side`: "right", "left", "both", or "unclear". Work it out
from the anatomy in front of you - the position of the disc relative to the
macula, which side the caruncle and medial canthus are on, the set of the nose
and brow, any laterality marking the printout itself carries. Put the landmark
you used in `side_reason`, so a wrong call can be seen and not just inherited.

Answer "unclear" when the image genuinely does not show enough to tell. Do not
guess: a candidate marked for describing the wrong eye is worse off than one
told nothing. But do not retreat to "unclear" out of caution when the landmark
is there - a station's marks are usually written per eye, and an unlabelled
photograph makes them unearnable.

If an abnormality is visible in only one of two eyes shown, that is
"one_eye_affected" in `affected`, however many eyes are pictured.

Name the modality as exactly one of: external, slit_lamp, fundus, angiogram,
oct, ultrasound, radiology, visual_field, topography, pathology, other. For
radiology say in `shows` whether it is CT or MRI, and which region.

The caption is read by a candidate sitting the station, so write it as English,
not as the field values above. Never put "one_eye", "both_eyes" or "unclear"
in it, and never "one eye", "an eye" or "one macula" either: a candidate who
cannot tell which eye they are looking at describes the wrong one and is
marked wrong for it. Name the side, or leave laterality out of the sentence
altogether and let `side` carry it.

Good captions:
  "Fundus photograph of the left eye"
  "Slit lamp photograph of both eyes"
  "Nine positions of gaze"
  "Axial MRI of the head"
  "Optical coherence tomography of the macula"

Bad captions, and why:
  "Slit lamp photograph of one eye"   - which one? this is the whole fault
  "Optical coherence tomography of one macula"  - same
  "External photograph of an eye"     - same

It must NOT name a diagnosis and must not describe the abnormality - the
candidate reads it before being asked to describe the image themselves.

Return ONLY a JSON object:
{
  "modality": "<one of the values above>",
  "laterality": "one_eye" | "both_eyes" | "unclear",
  "side": "right" | "left" | "both" | "unclear",
  "side_reason": "the landmark you read it from, one short phrase",
  "affected": "one_eye_affected" | "both_eyes_affected" | "none_visible" | "unclear",
  "panels": <how many separate photographs are tiled together, 1 if a single image>,
  "shows": "what is visible, one sentence, no diagnosis",
  "caption": "a neutral caption naming modality, laterality and view only"
}"""


# "of one eye", "in an eye", "of one macula" - laterality that names no side.
# The station marks per eye, so this tells the candidate nothing they can use.
_VAGUE_SIDE_RE = re.compile(
    r"\s*\b(?:of|in|from|showing)?\s*\b(?:one|an|a|the)\s+"
    r"(eyes?|macula|eye'?s?|optic\s+disc|disc)\b",
    re.IGNORECASE,
)
_SIDE_WORDS = {"right": "the right", "left": "the left", "both": "both"}


def label_side(caption: str | None, side: str | None) -> str | None:
    """Put the eye into the caption, or take the empty claim out of it.

    The blind pass writes the caption with the station withheld, which is the
    point of it - a caption that echoes what was expected hides the mismatch
    the check exists to find. But its output was then shown to the candidate as
    the label on the image, and "Slit lamp photograph of one eye" appeared 91
    times in the bank.

    Station 155 is what that costs. Its one usable photograph was captioned "of
    one eye"; the candidate saw a corneal graft, said right eye, and it was the
    left. The examiner's comment reads "the candidate incorrectly stated the
    graft was in the right eye" - marked wrong for a fact the screen would not
    tell them, and eight marks gone.

    So the side is written in where it is known and the empty phrase is struck
    out where it is not. Saying nothing is honest; "one eye" is a label that
    looks like information and carries none.
    """
    text = (caption or "").strip()
    if not text:
        return None
    named = _SIDE_WORDS.get((side or "").strip().lower())

    def swap(match: re.Match[str]) -> str:
        if not named:
            return ""
        # Keep the structure the caption named: an OCT is of a macula, not of
        # an eye, and "of the left eye" reads as a different picture.
        noun = match.group(1).lower().replace("eye's", "eye").rstrip("s")
        if noun in {"eye"}:
            noun = "eye" if named != "both" else "eyes"
        elif named == "both":
            noun = f"{noun}e" if noun.endswith("l") else f"{noun}s"
        return f" of {named} {noun}"

    fixed = _VAGUE_SIDE_RE.sub(swap, text, count=1).strip()
    # Never let the substitution eat the whole caption.
    return (fixed or text).rstrip(" ,;:") or None


_REQUEST_SIDE_RE = re.compile(r"\bboth\b|\bbilateral\b|\bright\b|\bleft\b", re.IGNORECASE)


def side_from_request(wanted: str | None) -> str | None:
    """The eye this figure was gone looking for, if its request named one.

    The blind pass reads the image and is right to answer "unclear" when the
    anatomy does not show which eye it is - most slit lamp photographs do not.
    But the figure was searched for "the signs of a DALK - left eye", and it is
    standing in for the left eye whatever a stranger's photograph happens to
    be: that is the view the candidate is being asked to read.

    Only ever a fallback. What the image actually shows wins, because a caption
    that contradicts the picture is worse than one that says less.
    """
    found = _REQUEST_SIDE_RE.findall(wanted or "")
    if not found:
        return None
    names = {f.lower() for f in found}
    if names & {"both", "bilateral"} or {"right", "left"} <= names:
        return "both"
    return "right" if "right" in names else "left"


def describe_blind(client: AIClient, data: bytes, media_type: str) -> dict[str, Any]:
    """Ask what the image shows without telling the model what to expect.

    The gate asks "does this show these signs?", which primes the answer it
    gets: a montage of one patient's unilateral Brown's syndrome was captioned
    "bilateral" at confidence 1.00, and the notes stored beneath it restated
    the station's own recorded findings almost word for word. It had not looked
    and concluded bilateral; it had been told to expect bilateral and agreed.

    Nothing here mentions the station, so agreement is not available. It runs
    once, on the image about to be attached, rather than on every candidate
    screened - a station's worth of screening is up to eighteen calls and this
    is one.

    The caption matters more than it used to: questions are now matched to
    their images by what the caption says, so a caption that echoes the request
    hides exactly the mismatch that check exists to find.
    """
    content = [
        TextPart("Describe the image below."),
        ImagePart(data=data, media_type=media_type),
    ]
    result = client.complete_json(task="vision", system=BLIND_SYSTEM, user=content)
    return result if isinstance(result, dict) else {}


_BILATERAL = re.compile(r"\b(both|bilateral|each eye|either eye|OU)\b", re.I)


def blind_disagreement(blind: dict[str, Any], wanted: str | None, station: OsceStation) -> str | None:
    """A disagreement between the blind description and what was asked for.

    Only the two axes an image can be checked on without clinical judgement:
    how many eyes are affected, and which imaging modality this is. Both are
    reported by a model that was not told what to expect, so agreement here is
    evidence rather than compliance.

    Returns a note, not a verdict. The caller downgrades a tier and records it;
    nothing is rejected on this. Rejecting more is what once left stations
    showing nothing at all, and a picture with an honest caption beside it is
    worth more than a gap.
    """
    if not blind:
        return None
    expectation = " ".join(
        t for t in (wanted, station.findings_elicited, station.findings) if t
    )

    if _BILATERAL.search(expectation) and blind.get("affected") == "one_eye_affected":
        return "the station describes both eyes; only one is affected in this image"

    # Only where `wanted` is a request for a particular investigation - "CT scan
    # of the orbits". A figure that named no view carries the station's whole
    # findings blob instead, and `expected_modalities_for` then guesses from
    # whatever words happen to be in it: "anterior segment and an optic nerve
    # pigmented lesion" yields external/slit_lamp/topography and calls the
    # correct fundus photograph wrong, and "corneal neovascularisation" yields
    # angiogram and calls the correct slit lamp photograph wrong. Comparing a
    # modality against a guess produced 146 false disagreements on the first
    # sweep. Laterality above is safe either way: the findings text really does
    # say whether both eyes are involved.
    if not (wanted or "").strip():
        return None
    expected = expected_modalities_for(station, wanted)
    seen = str(blind.get("modality") or "").strip().lower()
    if expected and seen and seen not in expected:
        return f"this is a {seen} image; the question asks for {'/'.join(sorted(expected))}"
    return None
