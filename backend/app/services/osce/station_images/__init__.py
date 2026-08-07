"""Source and verify clinical images for OSCE stations.

The real OSCE puts a live patient in front of the candidate. A station with no
image cannot test visual recognition, so images are searched for using the
station's own findings - but a generic web photograph of "hypermature cataract"
will not show *this* patient's inferior subluxation. An image that contradicts
the rubric is worse than none, because the candidate is then marked down for
correctly describing what they can actually see.

Every candidate image is therefore checked by a vision model against the
station's elicited findings, and rejected unless it genuinely shows them.

This was one 1759-line module. It is now a package, and this file re-exports
what that module held, so no caller had to change:

    constants  thresholds and job names
    queries    findings -> something worth searching for
    verify     the gate: does this photograph show the findings?
    describe   words for a station no photograph can be found for
    sourcing   the opening view, the coverage views, the per-question images
    ingested   figures that arrived inside a report rather than off the web
    settle     deciding a station has the images it is going to get
    jobs       the chunked job that sources a whole bank

The dependencies run one way. `constants`, `queries` and `verify` depend on
nothing else here; `describe` and `ingested` sit on those; `sourcing` and
`settle` on those again; `jobs` on top.

Patching this pipeline in a test means patching the name where it is looked up
- `station_images.sourcing.build_provider`, not this module. Re-exporting a
name here does not make it the one the function resolves.
"""

from __future__ import annotations

from app.services.osce.station_images.constants import (  # noqa: F401
    ANCILLARY_MODALITIES,
    FROM_PAPER,
    JOB_DESCRIBE_STATION_FIGURES,
    JOB_SETTLE_STATIONS,
    JOB_SOURCE_STATION_IMAGES,
    JOB_VERIFY_STATION_FIGURES,
    MIN_MATCH_CONFIDENCE,
    MIN_REPRESENTATIVE_CONFIDENCE,
    NOT_CLINICAL,
    REVIEWABLE_STATUSES,
    SETTLED_MATCH_CONFIDENCE,
    UNCHECKED_STATUSES,
)
from app.services.osce.station_images.queries import (  # noqa: F401
    QUERY_SYSTEM,
    _GAZE_POSITION_RE,
    _MONTAGE_RE,
    _gaze_first,
    build_search_queries,
    wants_gaze_montage,
)
from app.services.osce.station_images.verify import (  # noqa: F401
    VERIFY_SYSTEM,
    _CONCLUSION_RE,
    _DIAGNOSIS_STOPWORDS,
    _GENERIC_WORDS,
    _ROOT,
    _grounded,
    _words,
    expected_modalities_for,
    grounding_problem,
    leaked_term,
    verbatim_findings_floor,
    verify_image,
)
from app.services.osce.station_images.describe import (  # noqa: F401
    DESCRIBE_SYSTEM,
    DescriptionUnavailable,
    _queue_settle,
    describe_findings,
    figures_needing_description,
    handle_describe_station_figures,
)
from app.services.osce.station_images.sourcing import (  # noqa: F401
    _attach,
    opening_figures,
    opening_image_is_settled,
    source_coverage_images,
    source_image_for_station,
    source_prompt_images,
    stations_needing_images,
)
from app.services.osce.station_images.ingested import (  # noqa: F401
    bind_ingested_figures_to_questions,
    bound_figure_ids,
    handle_verify_station_figures,
    stations_with_unchecked_figures,
    verify_ingested_figures,
)
from app.services.osce.station_images.settle import (  # noqa: F401
    handle_settle_stations,
    settle_station,
)
from app.services.osce.station_images.jobs import (  # noqa: F401
    _queue_description_of_gaps,
    handle_source_station_images,
)

__all__ = [
    "ANCILLARY_MODALITIES",
    "DESCRIBE_SYSTEM",
    "DescriptionUnavailable",
    "FROM_PAPER",
    "JOB_DESCRIBE_STATION_FIGURES",
    "JOB_SETTLE_STATIONS",
    "JOB_SOURCE_STATION_IMAGES",
    "JOB_VERIFY_STATION_FIGURES",
    "MIN_MATCH_CONFIDENCE",
    "MIN_REPRESENTATIVE_CONFIDENCE",
    "NOT_CLINICAL",
    "QUERY_SYSTEM",
    "REVIEWABLE_STATUSES",
    "SETTLED_MATCH_CONFIDENCE",
    "UNCHECKED_STATUSES",
    "VERIFY_SYSTEM",
    "_CONCLUSION_RE",
    "_DIAGNOSIS_STOPWORDS",
    "_GAZE_POSITION_RE",
    "_GENERIC_WORDS",
    "_MONTAGE_RE",
    "_ROOT",
    "_attach",
    "_gaze_first",
    "_grounded",
    "_queue_description_of_gaps",
    "_queue_settle",
    "_words",
    "bind_ingested_figures_to_questions",
    "bound_figure_ids",
    "build_search_queries",
    "describe_findings",
    "expected_modalities_for",
    "figures_needing_description",
    "grounding_problem",
    "handle_describe_station_figures",
    "handle_settle_stations",
    "handle_source_station_images",
    "handle_verify_station_figures",
    "leaked_term",
    "opening_figures",
    "opening_image_is_settled",
    "settle_station",
    "source_coverage_images",
    "source_image_for_station",
    "source_prompt_images",
    "stations_needing_images",
    "stations_with_unchecked_figures",
    "verbatim_findings_floor",
    "verify_image",
    "verify_ingested_figures",
    "wants_gaze_montage",
]
