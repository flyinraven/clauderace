"""Domain constants for the RANZCO RACE examination.

Timings and mark allocations here are taken directly from the RANZCO RACE
candidate guidance and must not be changed casually - the simulator's whole
value is that it matches the real exam.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- Subspecialties ------------------------------------------------------
# The nine official ophthalmic subspecialties examined in RACE.
SUBSPECIALTIES: list[str] = [
    "Cataract",
    "Cornea & External Eye",
    "Glaucoma",
    "Neuro-ophthalmology",
    "Ocular Inflammation",
    "Ocular Motility",
    "Oculoplastics & Orbit",
    "Paediatrics",
    "Vitreoretinal",
]

# Free-text topic labels used by RANZCO in the examiners' reports do not always
# match our canonical list. Ingestion maps them through this table.
SUBSPECIALTY_ALIASES: dict[str, str] = {
    "cataract": "Cataract",
    "cataract surgery": "Cataract",
    "cornea": "Cornea & External Eye",
    "cornea and external eye": "Cornea & External Eye",
    "external eye": "Cornea & External Eye",
    "crosslinking": "Cornea & External Eye",
    "glaucoma": "Glaucoma",
    "neuro-ophthalmology": "Neuro-ophthalmology",
    "neuro ophthalmology": "Neuro-ophthalmology",
    "neuro": "Neuro-ophthalmology",
    "uveitis": "Ocular Inflammation",
    "ocular inflammation": "Ocular Inflammation",
    "inflammation": "Ocular Inflammation",
    "ocular motility": "Ocular Motility",
    "motility": "Ocular Motility",
    "extraocular motility": "Ocular Motility",
    "strabismus": "Ocular Motility",
    "oculoplastics": "Oculoplastics & Orbit",
    "oculoplastic": "Oculoplastics & Orbit",
    "oculoplastics and orbit": "Oculoplastics & Orbit",
    "orbit": "Oculoplastics & Orbit",
    "oculoplastic and orbit": "Oculoplastics & Orbit",
    "paediatrics": "Paediatrics",
    "paediatric": "Paediatrics",
    "pediatrics": "Paediatrics",
    "retina": "Vitreoretinal",
    "vitreoretinal": "Vitreoretinal",
    "medical retina": "Vitreoretinal",
    "surgical retina": "Vitreoretinal",
    "vr": "Vitreoretinal",
}


def normalise_subspecialty(raw: str | None) -> str | None:
    """Map a free-text topic to one of the nine canonical subspecialties."""
    if not raw:
        return None
    key = raw.strip().lower().strip(".:-")
    if key in SUBSPECIALTY_ALIASES:
        return SUBSPECIALTY_ALIASES[key]
    for alias, canonical in SUBSPECIALTY_ALIASES.items():
        if alias in key:
            return canonical
    return None


# --- Question types -------------------------------------------------------
QUESTION_SEQ = "SEQ"
QUESTION_VSAQ = "VSAQ"
QUESTION_OSCE = "OSCE"

SEQ_TOTAL_MARKS = 20
VSAQ_TOTAL_MARKS = 2

# --- Lifecycle ------------------------------------------------------------
STATUS_DRAFT = "draft"
STATUS_REVIEW = "review"
STATUS_APPROVED = "approved"
STATUS_ARCHIVED = "archived"

SOURCE_PAST_PAPER = "past_paper"
SOURCE_GENERATED = "generated"
SOURCE_MANUAL = "manual"

ROLE_STUDENT = "student"
ROLE_ADMIN = "admin"


# --- Exam structure and timing -------------------------------------------
@dataclass(frozen=True)
class PaperSpec:
    """Structure and clock for one written paper."""

    number: int
    day: int
    seq_count: int
    vsaq_count: int
    prep_minutes: int
    reading_minutes: int
    writing_minutes: int

    @property
    def total_marks(self) -> int:
        return self.seq_count * SEQ_TOTAL_MARKS + self.vsaq_count * VSAQ_TOTAL_MARKS

    @property
    def total_minutes(self) -> int:
        return self.prep_minutes + self.reading_minutes + self.writing_minutes


# Day 1: Paper 1 then a 30-minute supervised break then Paper 2 (3h total).
# Day 2: Papers 3 and 4 mirror Papers 1 and 2.
PAPER_SPECS: dict[int, PaperSpec] = {
    1: PaperSpec(1, 1, seq_count=5, vsaq_count=15, prep_minutes=5, reading_minutes=15, writing_minutes=100),
    2: PaperSpec(2, 1, seq_count=4, vsaq_count=15, prep_minutes=5, reading_minutes=15, writing_minutes=80),
    3: PaperSpec(3, 2, seq_count=5, vsaq_count=15, prep_minutes=5, reading_minutes=15, writing_minutes=100),
    4: PaperSpec(4, 2, seq_count=4, vsaq_count=15, prep_minutes=5, reading_minutes=15, writing_minutes=80),
}

SUPERVISED_BREAK_MINUTES = 30

# Exam phases, in order.
PHASE_NOT_STARTED = "not_started"
PHASE_PREP = "prep"
PHASE_READING = "reading"
PHASE_WRITING = "writing"
PHASE_SUBMITTED = "submitted"
PHASE_EXPIRED = "expired"

# Answers submitted within this many seconds of the deadline are still accepted,
# to absorb network latency and a Render cold start on the final save.
SUBMISSION_GRACE_SECONDS = 30

# --- OSCE -----------------------------------------------------------------
OSCE_STATION_COUNT = 18
OSCE_STATION_MINUTES = 9

# --- Grading --------------------------------------------------------------
# Two examiners mark every question in the real exam; we simulate that with two
# independent grading passes. If they disagree by more than this fraction of the
# available marks, the part is flagged for review.
EXAMINER_DISCREPANCY_THRESHOLD = 0.15

# Fallback Angoff expectation (fraction of marks a borderline candidate scores)
# used when a question has not been rated.
DEFAULT_ANGOFF_EXPECTED = 0.5
