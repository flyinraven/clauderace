"""Whether an image is of the right *kind* to answer what is being asked.

A vision model grading an image against a station's findings will happily pass
a fluorescein angiogram for a station whose findings mention the disc, even
when the candidate's task is "examine the anterior segment". It is answering
"does this show the pathology?" when the question that matters is "is this the
examination the candidate was asked to perform?".

That second question is structural, not clinical: an anterior segment task can
never be answered by a posterior segment image, whatever the pathology. So it
is settled here, deterministically, and the model is only asked to name what it
is looking at.
"""

from __future__ import annotations

import re

# The modality vocabulary the vision model must answer in. Kept short: every
# extra option is another way for it to disagree with itself.
MODALITIES = (
    "external",       # face, lids, ocular surface at arm's length
    "slit_lamp",      # anterior segment under the slit lamp, gonioscopy
    "fundus",         # fundus / retinal photograph, including wide-field
    "angiogram",      # fluorescein or ICG angiography
    "oct",            # OCT of any structure, including OCT-A
    "ultrasound",     # A- or B-scan
    "radiology",      # CT, MRI, X-ray, orbital imaging
    "visual_field",   # perimetry printouts
    "topography",     # corneal topography / tomography maps
    "pathology",      # histology, cytology
    "other",
)

# What a region of the eye can actually be photographed with. A task naming a
# region is answerable by any of these and by nothing else.
# What the candidate can see by looking at the patient, with the instruments a
# station puts in front of them. Everything else is an investigation: it was
# ordered, performed and printed, and at a real station the examiner hands it
# over when the candidate asks for it.
#
# The distinction is the whole shape of an OSCE. The mock station for Joshua
# Bullock reads "How would you confirm the diagnosis? - ask for
# Pentacam/Anterion. Anterion images supplied in powerpoint": the map is the
# reward for asking. Ours opened station 155 on four topography maps and buried
# the one slit lamp photograph its eight-mark rubric was written for, which
# both gave away the answer and hid the question.
#
# "other" is unclassified rather than investigational - a diagram, a photograph
# the vision model could not name - so it is left where it is.
PATIENT_VIEW_MODALITIES = frozenset({"external", "slit_lamp", "fundus", "other"})


def is_investigation(modality: str | None) -> bool:
    """Whether this is something handed over on request, not seen by looking."""
    name = (modality or "").strip().lower()
    return bool(name) and name not in PATIENT_VIEW_MODALITIES


_REGION_MODALITIES: dict[str, frozenset[str]] = {
    "anterior": frozenset({"external", "slit_lamp", "topography"}),
    "posterior": frozenset({"fundus", "angiogram", "oct", "ultrasound"}),
    "adnexal": frozenset({"external", "slit_lamp", "radiology"}),
}

# Region wording, checked before modality wording so that "fundus photograph"
# is read as an explicit modality rather than merely a posterior region.
_REGION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("anterior", re.compile(
        r"\banterior\s+segment\b|\bslit[- ]?lamp\b|\bcornea\w*\b|\bconjunctiv\w+\b|"
        r"\bsclera\w*\b|\biris\b|\bpupil\w*\b|\blens\b|\bcataract\b|\bkeratic\b|"
        r"\bhypopyon\b|\banterior\s+chamber\b|\bgonioscop\w+\b|\bcorneal\s+graft\b",
        re.IGNORECASE)),
    ("posterior", re.compile(
        r"\bposterior\s+segment\b|\bfundus\w*\b|\bretina\w*\b|\bmacula\w*\b|"
        r"\boptic\s+(?:disc|nerve\s+head)\b|\bvitreo?\w*\b|\bchoroid\w*\b|"
        r"\bdilated\s+fundus\b|\bophthalmoscop\w+\b",
        re.IGNORECASE)),
    ("adnexal", re.compile(
        r"\beyelid\w*\b|\blids?\b|\bptosis\b|\bproptosis\b|\borbit\w*\b|"
        r"\blacrimal\b|\bperiocular\b",
        re.IGNORECASE)),
)

# Explicitly named modalities. These win over region wording: "OCT of the
# macula" wants an OCT, not any posterior image.
_MODALITY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # ICGA, not just ICG: the A is part of the acronym, so a word boundary after
    # "ICG" never matched it and one station's third investigation went unread.
    ("angiogram", re.compile(r"\bfluorescein\b|\bangiogra\w+\b|\bFFA\b|\bICGA?\b|\bFA\b")),
    ("oct", re.compile(r"\bOCT\b|\boptical\s+coherence\b|\bOCT[- ]?A\b", re.IGNORECASE)),
    ("radiology", re.compile(r"\bCT\b|\bMRI\b|\bX[- ]?ray\b|\bradiograph\w*\b|\bscan\s+of\s+the\s+orbits?\b")),
    # UBM is an ultrasound, and naming no modality at all let a station's
    # request for one be answered by a corneal topography of the same region.
    # Biometry belongs here: it is an ultrasound or optical measurement, and
    # naming no modality at all meant "corneal topography and biometry for both
    # eyes" never split - the search went looking for one image that was both a
    # Pentacam map and an IOL Master printout, which does not exist.
    ("ultrasound", re.compile(r"\bultrasound\b|\bB[- ]?scan\b|\bA[- ]?scan\b|\bechograph\w+\b|"
                              r"\bUBM\b|\bultrasound\s+biomicroscop\w+\b|\bbiometry\b|"
                              r"\bIOL\s?Master\b|\bLenstar\b|\baxial\s+length\b", re.IGNORECASE)),
    ("visual_field", re.compile(r"\bvisual\s+field\w*\b|\bperimetr\w+\b|\bHumphrey\b|\bGoldmann\b", re.IGNORECASE)),
    ("topography", re.compile(r"\btopograph\w+\b|\btomograph\w+\b|\bPentacam\b|\bOrbscan\b", re.IGNORECASE)),
    ("pathology", re.compile(r"\bhistolog\w+\b|\bhistopatholog\w+\b|\bbiops\w+\b|\bcytolog\w+\b", re.IGNORECASE)),
    ("fundus", re.compile(r"\bfundus\s+(?:photograph|photo|image)\w*\b|\bretinal\s+photograph\w*\b|"
                          r"\bwide[- ]?field\s+(?:photo|image)\w*\b|"
                          # Autofluorescence is taken with a fundus camera and
                          # is what the vision model calls it. Two stations ask
                          # for FAF, and without this it named no modality at
                          # all, so a compound request would not split on it.
                          r"\bauto[- ]?fluorescence\b|\bFAF\b", re.IGNORECASE)),
    ("slit_lamp", re.compile(r"\bslit[- ]?lamp\s+(?:photograph|photo|image)\w*\b", re.IGNORECASE)),
    ("external", re.compile(r"\bexternal\s+(?:photograph|photo|image|eye)\w*\b|"
                            r"\bclinical\s+photograph\s+of\s+the\s+face\b|"
                            r"\bnine\s+positions?\s+of\s+gaze\b|\bcover\s+test\b", re.IGNORECASE)),
)


# Examinations that exist only as a sequence of positions. A motility deficit,
# a cranial nerve palsy or a squint IS the difference between the gaze
# positions, so one primary-position photograph shows a patient who looks
# ordinary: "examine the ocular motility of the right eye" cannot be answered
# from it, and every duction mark in the rubric is unearnable.
_GAZE_RE = re.compile(
    r"\bmotilit\w+\b|\bduction\w*\b|\bversion\w*\b|\bgaze\b|\bsquint\w*\b|"
    # How the task is most often worded: "examine the ocular movements".
    r"\b(?:eye|ocular|extraocular)\s+movements?\b|"
    r"\bstrabismus\b|\beso[- ]?tropi\w+\b|\bexo[- ]?tropi\w+\b|\bhyper[- ]?tropi\w+\b|"
    r"\bhypo[- ]?tropi\w+\b|\bnystagmus\b|\bcover\s+test\b|\bdiplopia\b|"
    r"\bunder[- ]?action\w*\b|\bover[- ]?action\w*\b|\bover[- ]?elevation\b|"
    r"\bDuane\w*\b|\bBrown'?s\s+syndrome\b|\bophthalmopleg\w+\b|"
    r"\b(?:third|fourth|sixth|III|IV|VI)\s+(?:cranial\s+)?nerve\b|"
    r"\b(?:cranial\s+)?nerve\s+pals\w+\b|"
    # The extraocular muscles, named the way a rubric names them: "deficits in
    # right MR, SR, IR".
    r"\b(?:MR|LR|SR|IR|SO|IO)\b|\bmedial\s+rectus\b|\blateral\s+rectus\b|"
    r"\bsuperior\s+rectus\b|\binferior\s+rectus\b|\bsuperior\s+oblique\b|"
    r"\binferior\s+oblique\b|\bextraocular\s+m\w+\b",
    re.IGNORECASE,
)

# How the montage is filed. Photographers publish it as the nine (sometimes
# five) positions of gaze, and `_MODALITY_PATTERNS` already reads that wording
# back as an external photograph.
GAZE_PHRASE = "external photograph montage of the nine positions of gaze"


def wants_gaze_positions(*texts: str | None) -> bool:
    """Whether this task can only be shown as a montage of gaze positions."""
    blob = " ".join(t for t in texts if t)
    return bool(blob.strip()) and bool(_GAZE_RE.search(blob))


def expected_modalities(*texts: str | None) -> frozenset[str]:
    """The modalities that could answer this task, or empty if it does not say.

    An explicitly named modality is taken literally. Failing that, the region
    being examined decides. Empty means no constraint could be read from the
    wording, and the caller must not gate on it — a filter that guesses is
    worse than no filter, because it silently discards good images.
    """
    blob = " ".join(t for t in texts if t)
    if not blob.strip():
        return frozenset()

    named = {name for name, pattern in _MODALITY_PATTERNS if pattern.search(blob)}
    if named:
        # A fundus photograph and its angiogram are routinely shown together,
        # and a station naming both should accept either.
        return frozenset(named)

    regions = {name for name, pattern in _REGION_PATTERNS if pattern.search(blob)}
    allowed: set[str] = set()
    for region in regions:
        allowed |= _REGION_MODALITIES[region]
    # A motility task is an examination in its own right, and one no scan can
    # answer. Without this, "examine the ocular motility of the right eye" named
    # neither a modality nor a region, so it was unconstrained - and, upstream,
    # generated no view at all.
    #
    # Only when nothing else was read: a task that named the fundus and happens
    # to mention diplopia still wants a fundus photograph, and widening it to
    # external would let a face shot answer a retinal question.
    if not allowed and _GAZE_RE.search(blob):
        allowed.add("external")
    return frozenset(allowed)


def modality_mismatch(expected: frozenset[str], observed: str | None) -> str | None:
    """Why `observed` cannot answer a task wanting `expected`, or None if it can.

    Unknown or unconstrained cases pass: this gate only ever rejects on a
    positive contradiction.
    """
    if not expected or not observed:
        return None
    seen = str(observed).strip().lower().replace(" ", "_").replace("-", "_")
    if seen in {"", "other", "unknown"} or seen not in MODALITIES:
        return None
    if seen in expected:
        return None
    return (
        f"shows {seen.replace('_', ' ')}, but the task calls for "
        f"{' or '.join(sorted(m.replace('_', ' ') for m in expected))}"
    )


# How to ask a librarian for each modality. Used to build the search phrase and
# the caption, so the words are the ones a clinical image would be filed under.
MODALITY_PHRASES: dict[str, str] = {
    "external": "external photograph",
    "slit_lamp": "slit lamp photograph",
    "fundus": "fundus photograph",
    "angiogram": "fluorescein angiogram",
    "oct": "OCT",
    "ultrasound": "ocular ultrasound",
    "radiology": "MRI or CT",
    "visual_field": "visual field printout",
    "topography": "corneal topography",
    "pathology": "histopathology slide",
}

# Results that are numbers or a report, not something to look at. A candidate
# reads these; they cannot describe them from a photograph, and searching for
# one returns stock images of test tubes.
_NON_VISUAL_RESULT_RE = re.compile(
    r"\bblood\w*\b|\bserum\b|\bFBC\b|\bU\s*&\s*E\b|\bESR\b|\bCRP\b|\bACE\b|\bANCA\b|"
    r"\bANA\b|\bRPR\b|\bserolog\w+\b|\btitre\w*\b|\bculture\w*\b|\bPCR\b|\bswab\w*\b|"
    r"\bgenetic\s+test\w*\b|\bkaryotyp\w+\b|\bLFT\w*\b|\bHbA1c\b|\bQuantiFERON\b|"
    r"\bMantoux\b|\bbiochemistr\w+\b",
    re.IGNORECASE,
)


# A request for a drawing rather than a record of a patient. The verifier
# rejects diagrams outright and must keep doing so - an illustration does the
# candidate's describing for them - so a question asking for one can never be
# filled by searching, however many times it is tried. Station 111 asks for a
# diagram of a trabeculectomy: scleral flap, ostium, conjunctival closure.
_DRAWING_RE = re.compile(
    r"\bdiagram\w*\b|\billustrat\w+\b|\bschematic\w*\b|\bcartoon\w*\b|"
    r"\bdrawing\w*\b|\bflow[- ]?chart\b|\bgraph\s+of\b|\bannotated\s+sketch\b",
    re.IGNORECASE,
)


def unsourceable_reason(text: str | None) -> str | None:
    """Why no search can ever fill this request, or None if one might.

    Kept separate from "the search failed". A question whose investigation is a
    serology titre or a textbook diagram is not waiting on a better query - it
    is waiting on an administrator to reword it, and reporting the two the same
    way means paying for the impossible ones on every run.
    """
    if not text or not text.strip():
        return None
    if _NON_VISUAL_RESULT_RE.search(text):
        return "a result to be read, not an image"
    if _DRAWING_RE.search(text):
        return "a diagram, which is rejected as it does the describing for them"
    return None


# Every way these requests were actually written: sentences, semicolons,
# commas, and "and". Splitting on all of them over-splits wildly - "showing
# fluid and haemorrhage" becomes two - which is what the merge below is for.
_REQUEST_SPLIT_RE = re.compile(
    r"(?<=[a-z0-9)])\.\s+(?=[A-Z(])|\s*;\s*|,?\s+and\s+|\s*,\s*"
)


def split_investigations(text: str | None) -> list[str]:
    """One phrase per investigation the question asks the candidate to read.

    Half of the unfilled requests name two: "OCT of the right macula showing
    CNVM and fluorescein angiogram of both eyes showing multifocal choroiditis".
    No one image is both, so searching the whole string returns nothing and the
    question keeps asking for something that was never going to arrive.

    Only splits where each side names an investigation of its own. "OCT showing
    fluid and haemorrhage" is one image with two findings, and breaking it apart
    would buy two pictures of the same scan.
    """
    whole = text.strip()
    pieces = [p.strip(" .,;") for p in _REQUEST_SPLIT_RE.split(whole) if p and p.strip(" .,;")]

    # A piece that names no investigation is a continuation of the one before
    # it, not a request of its own: "...showing multifocal choroiditis lesions"
    # + "leakage in the right eye" is one angiogram. Without this the trailing
    # clause looked like an unidentifiable investigation and defeated the split
    # on half the stations that needed it.
    groups: list[str] = []
    for piece in pieces:
        if named_modality(piece) is None and groups:
            groups[-1] = f"{groups[-1]}, {piece}"
        else:
            groups.append(piece)

    # Two investigations, and genuinely different ones. One scan described at
    # length must stay whole, or the station buys two pictures of it.
    named = [named_modality(g) for g in groups]
    if len(groups) < 2 or any(m is None for m in named) or len(set(named)) < 2:
        return [whole]
    return groups


def named_modality(text: str | None) -> str | None:
    """The modality this text names outright, or None if it only implies one.

    Grouping a rubric on this is what separates "the OCT shows intraretinal
    fluid" from "the disc is swollen" - two findings in the same eye that no
    single photograph can carry. Region wording is deliberately not enough:
    "the macula" could be a fundus photograph or an OCT, and splitting on a
    guess would source two images where one would have done.
    """
    if not text or not text.strip():
        return None
    named = [name for name, pattern in _MODALITY_PATTERNS if pattern.search(text)]
    if not named:
        return None
    # Ordered as _MODALITY_PATTERNS is: the more specific investigations first,
    # so "OCT of the macula with a fundus photograph" files under OCT.
    return named[0]


def is_non_visual_result(text: str | None) -> bool:
    """Is this a result to be read rather than an image to be described?"""
    return bool(text and _NON_VISUAL_RESULT_RE.search(text))
