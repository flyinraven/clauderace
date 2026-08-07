"""Turning a station's findings into something worth searching for.

A query naming the diagnosis finds textbook illustrations; one naming the
visible signs finds photographs. Everything here is about that difference.
"""

from __future__ import annotations

import re
from sqlalchemy.orm import Session
from app.models import OsceFigure, OsceStation
from app.services.ai import AIClient, AIError
from app.services.imagesearch.relevance import wants_gaze_positions
from app.services.osce.coverage import station_views


QUERY_SYSTEM = """\
You write image search queries for ophthalmology teaching material.

Given an OSCE station's case and the clinical signs the candidate is meant to
see, write THREE search phrases, from most specific to most general. Searching
only for the full picture fails on stations that combine several findings, but
the underlying pathologies are common and easy to find on their own.

  1. specific  - modality plus the distinctive combination of signs
  2. core      - modality plus the single main diagnosis
  3. broad     - the diagnosis or classic sign alone, no modality

Name the modality as a photographer would: "slit lamp photograph", "fundus
photograph", "external eye photograph", "OCT", "fluorescein angiogram".

If the signs are a motility, gaze, squint or cranial nerve palsy deficit, keep
"nine positions of gaze" in phrases 1 and 2: the deficit is only visible across
the positions, and a single frontal photograph shows nothing to describe.

Never include incidental surgical hardware, laterality, or a second unrelated
condition in phrases 2 and 3 - those are what make a search return nothing.

Return ONLY a JSON object:
{"queries": ["specific phrase", "core phrase", "broad phrase"]}"""


def build_search_queries(
    db: Session, client: AIClient, station: OsceStation, wanted: str | None = None
) -> list[str]:
    """Three queries, specific to broad, tried in order until one yields a match.

    `wanted` narrows the search to one question's ancillary test - "OCT of the
    right macula showing intraretinal fluid" - instead of the station's signs
    as a whole. An examiner asking the candidate to read an OCT must be handed
    an OCT, not the external photograph that opens the station.
    """
    signs = wanted or station.findings_elicited or station.findings or ""
    fallback = [
        q for q in [
            wanted or " ".join(p for p in [station.diagnosis, "clinical photograph"] if p),
            station.diagnosis or "",
            station.subspecialty or "ophthalmology clinical photograph",
        ] if q
    ]
    try:
        data = client.complete_json(
            task="utility",
            system=QUERY_SYSTEM,
            user=(
                f"SUBSPECIALTY: {station.subspecialty or 'unknown'}\n"
                f"CASE: {station.case_summary or 'unknown'}\n"
                f"SIGNS THE CANDIDATE SHOULD SEE: {signs or 'unknown'}\n"
                f"DIAGNOSIS (for your context only): {station.diagnosis or 'unknown'}"
            ),
            max_tokens=200,
            temperature=0.2,
        )
        queries = [
            str(q).strip().strip('"')
            for q in (data.get("queries") if isinstance(data, dict) else data) or []
            if str(q).strip()
        ]
        return _gaze_first(queries or fallback, signs, station)
    except (AIError, ValueError, AttributeError):
        return _gaze_first(fallback, signs, station)


def _gaze_first(queries: list[str], signs: str, station: OsceStation) -> list[str]:
    """Put the montage phrasing in front, for a station about eye movement.

    Station 7 ran through all three of the model's phrasings and attached from
    the broad one, "multiple cranial nerve palsies" - which had dropped the gaze
    wording altogether and returned a face in primary position. The phrase a
    montage is actually filed under is short, fixed and free to write, so it
    does not need a model to guess it.
    """
    if not wants_gaze_positions(signs, station.diagnosis):
        return queries
    subject = station.diagnosis or station.subspecialty or "ocular motility"
    lead = [
        f"nine positions of gaze photograph {subject}",
        f"ocular motility photographs {subject}",
        "nine positions of gaze photograph montage",
    ]
    return lead + [q for q in queries if q not in lead]


# More than one picture of the eyes, as the vision model reports it. Both of
# these must match: a "multi-panel photograph, without and with glasses" is a
# montage of nothing to do with movement, and one photograph "in primary gaze"
# is the single position the whole check exists to catch.
_MONTAGE_RE = re.compile(
    r"\bmontage\b|\bmulti[- ]panel\b|\bnine[- ](?:panel|gaze)\b|\bpanels\b|"
    r"\b(?:series|serial|several|multiple|composite)\b.{0,40}\bphotograph",
    re.IGNORECASE,
)
_GAZE_POSITION_RE = re.compile(
    r"\bgaze\b|\bpositions?\s+of\s+gaze\b|\bup[- ]?gaze\b|\bdown[- ]?gaze\b|"
    r"\bpositions?\s+of\s+(?:the\s+)?eyes?\b|\bductions?\b|\bversions?\b",
    re.IGNORECASE,
)


def wants_gaze_montage(station: OsceStation, figure: OsceFigure) -> bool:
    """Whether this figure was sourced without knowing it needed gaze positions.

    A motility station whose opening photograph is a single primary-position
    face shot looks perfectly good to every other test here - right modality,
    right pathology, high confidence - and still cannot be described. The
    deficit only exists across the positions of gaze.

    What answers it is the description the vision model wrote when the image was
    attached, which is free to read and is the only stored record of how many
    panels the image has. Of the fourteen stations first flagged, five already
    had a nine-panel series the searcher had stumbled into, and re-sourcing those
    would have paid to replace a perfect image.

    Deliberately NOT read from `wanted_description`. Asking for the montage was
    the first test written, and it answered the wrong question: after a
    re-source the phrase is there whether or not a montage was found, so five
    stations still showing one position went quiet the moment they had been
    searched. What matters is the image, not the request.

    An image with nothing recorded about it counts as needing one. That is the
    figure sourced before any of this, which is exactly the case to look at.
    """
    views = station_views(station)
    if not views or not views[0].gaze:
        return False
    described = f"{figure.verification_notes or ''} {figure.caption or ''}"
    return not (_MONTAGE_RE.search(described) and _GAZE_POSITION_RE.search(described))
