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
    ("angiogram", re.compile(r"\bfluorescein\b|\bangiogra\w+\b|\bFFA\b|\bICG\b|\bFA\b")),
    ("oct", re.compile(r"\bOCT\b|\boptical\s+coherence\b|\bOCT[- ]?A\b", re.IGNORECASE)),
    ("radiology", re.compile(r"\bCT\b|\bMRI\b|\bX[- ]?ray\b|\bradiograph\w*\b|\bscan\s+of\s+the\s+orbits?\b")),
    ("ultrasound", re.compile(r"\bultrasound\b|\bB[- ]?scan\b|\bA[- ]?scan\b|\bechograph\w+\b", re.IGNORECASE)),
    ("visual_field", re.compile(r"\bvisual\s+field\w*\b|\bperimetr\w+\b|\bHumphrey\b|\bGoldmann\b", re.IGNORECASE)),
    ("topography", re.compile(r"\btopograph\w+\b|\btomograph\w+\b|\bPentacam\b|\bOrbscan\b", re.IGNORECASE)),
    ("pathology", re.compile(r"\bhistolog\w+\b|\bhistopatholog\w+\b|\bbiops\w+\b|\bcytolog\w+\b", re.IGNORECASE)),
    ("fundus", re.compile(r"\bfundus\s+(?:photograph|photo|image)\w*\b|\bretinal\s+photograph\w*\b|"
                          r"\bwide[- ]?field\s+(?:photo|image)\w*\b", re.IGNORECASE)),
    ("slit_lamp", re.compile(r"\bslit[- ]?lamp\s+(?:photograph|photo|image)\w*\b", re.IGNORECASE)),
    ("external", re.compile(r"\bexternal\s+(?:photograph|photo|image|eye)\w*\b|"
                            r"\bclinical\s+photograph\s+of\s+the\s+face\b|"
                            r"\bnine\s+positions?\s+of\s+gaze\b|\bcover\s+test\b", re.IGNORECASE)),
)


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
    if not regions:
        return frozenset()
    allowed: set[str] = set()
    for region in regions:
        allowed |= _REGION_MODALITIES[region]
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
