"""Which stations cannot be answered from the images they have. Read-only.

A station is marked on what the candidate describes. If the rubric awards marks
for a sign no image shows, those marks are unearnable and the station is not
hard, it is impossible.

Nothing here calls a model or an image search, so it costs nothing to run. Every
judgement is made from what is already stored: the rubric, the views it implies,
and the verdict the vision model wrote when the image was first attached -
including the list of signs it recorded the image does NOT show.

Use it to pick the stations worth re-sourcing rather than re-sourcing all of
them, which spends on Brave and the vision model for images that were fine.

    python scripts/audit_station_images.py --database-url "postgresql+psycopg://..."
    python scripts/audit_station_images.py --from-env --ids-only
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.models import OsceStation  # noqa: E402
from app.services.imagesearch.relevance import is_non_visual_result  # noqa: E402
from app.services.osce.coverage import station_views  # noqa: E402
from app.services.osce.station_images import (  # noqa: E402
    SETTLED_MATCH_CONFIDENCE,
    wants_gaze_montage,
)


def url_from_env() -> str:
    env = BACKEND / ".env.production"
    if not env.exists():
        raise SystemExit(f"{env} not found; pass --database-url instead")
    match = re.search(r"^DATABASE_URL\s*=\s*(.+)$", env.read_text(encoding="utf-8-sig"), re.M)
    if not match:
        raise SystemExit("No DATABASE_URL in backend/.env.production")
    return match.group(1).strip().strip("\"'")


def faults_for(station: OsceStation) -> list[str]:
    """Every reason this station's marks cannot currently be earned."""
    faults: list[str] = []
    views = station_views(station)
    with_image = [f for f in station.figures if f.image_id is not None]

    if views and not with_image:
        faults.append(f"no image at all, and the rubric needs {len(views)}")
    elif len(with_image) < len(views):
        faults.append(
            f"{len(with_image)} image(s) for {len(views)} view(s) the rubric needs"
        )

    for figure in with_image:
        label = f"figure {figure.position}"
        if figure.verification_status == "representative":
            missing = ""
            note = figure.verification_notes or ""
            found = re.search(r"\[Does NOT show:\s*(.+?)\]", note, re.S)
            if found:
                missing = f": missing {found.group(1).strip()[:90]}"
            faults.append(f"{label} is representative only{missing}")
        elif (figure.match_confidence or 1.0) < SETTLED_MATCH_CONFIDENCE:
            faults.append(
                f"{label} scraped in at {figure.match_confidence:.0%} confidence"
            )
        if not figure.is_approved:
            faults.append(f"{label} is not approved, so nothing is shown for it")

    opening = min(with_image, key=lambda f: f.position, default=None)
    if opening is not None and wants_gaze_montage(station, opening):
        faults.append(
            f"figure {opening.position} was sourced as a single photograph, but the task "
            f"examines ocular motility and needs the positions of gaze"
        )

    # A sub-question that asks the candidate to read an investigation, with no
    # figure bound to it, is a question about an image that is not there.
    for prompt in station.prompts or []:
        wanted = str(prompt.get("image_wanted") or "").strip()
        if wanted and not prompt.get("figure_id"):
            if is_non_visual_result(wanted):
                faults.append(
                    f"question {prompt.get('label')} wants '{wanted[:40]}', which is a "
                    f"result to be read, not an image"
                )
            else:
                faults.append(f"question {prompt.get('label')} has no image for its investigation")

    return faults


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--database-url")
    group.add_argument("--from-env", action="store_true")
    parser.add_argument(
        "--ids-only", action="store_true",
        help="print just the station ids, to feed a re-source",
    )
    args = parser.parse_args()

    engine = create_engine(url_from_env() if args.from_env else args.database_url)
    flagged: list[tuple[OsceStation, list[str]]] = []

    with Session(engine) as db:
        stations = db.execute(select(OsceStation).order_by(OsceStation.id)).scalars().all()
        for station in stations:
            faults = faults_for(station)
            if faults:
                flagged.append((station, faults))

    if args.ids_only:
        print(" ".join(str(s.id) for s, _ in flagged))
        return 0

    for station, faults in flagged:
        name = station.title or f"Station {station.station_number or station.id}"
        print(f"#{station.id:>4} {name[:44]:44s} {station.exam_period or '-'}")
        for fault in faults:
            print(f"       - {fault}")

    total = len(stations)
    print(
        f"\n{len(flagged)} of {total} station(s) cannot be fully answered from their "
        f"images; {total - len(flagged)} are fine and need no re-sourcing."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
