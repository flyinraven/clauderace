"""Thresholds and job names shared across the image pipeline.

Here rather than in whichever module happens to use each one first, so nothing
in the package imports a sibling just to read a number.
"""

from __future__ import annotations


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


JOB_VERIFY_STATION_FIGURES = "verify_station_figures"

# What ingest wrote before its figures were checked. Anything else has been
# through a vision model and must not be re-graded for free.
UNCHECKED_STATUSES = frozenset({"verified", "unverified", "", None})

# Investigations the examiner hands over, as against what they see looking at
# the patient. A station's recorded findings are the bedside examination, so
# they can stand in for a missing photograph of it - and cannot stand in for
# one of these, which show something the findings never described.
ANCILLARY_MODALITIES = frozenset(
    {"oct", "angiogram", "radiology", "visual_field", "ultrasound", "topography", "pathology"}
)

# Written by the rule that used to drop a paper's own figures. Kept only so the
# rows carrying it can be found and reconsidered - nothing sets it now.
NOT_CLINICAL = "not_clinical"

# Taken from the examiners' report, and shown on that basis.
FROM_PAPER = "from_paper"

# What a re-verification pass will look at again. "rejected" is in here because
# it used to mean two different things: a chart, and a real photograph of an
# investigation the opening task did not ask for. The second is now kept and
# shown, so those have to be reconsidered rather than left dark for ever.
REVIEWABLE_STATUSES = UNCHECKED_STATUSES | {"rejected", NOT_CLINICAL}



JOB_DESCRIBE_STATION_FIGURES = "describe_station_figures"


JOB_SETTLE_STATIONS = "settle_stations"
