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
from app.services.osce.sittability import station_faults  # noqa: E402


def url_from_env() -> str:
    env = BACKEND / ".env.production"
    if not env.exists():
        raise SystemExit(f"{env} not found; pass --database-url instead")
    match = re.search(r"^DATABASE_URL\s*=\s*(.+)$", env.read_text(encoding="utf-8-sig"), re.M)
    if not match:
        raise SystemExit("No DATABASE_URL in backend/.env.production")
    return match.group(1).strip().strip("\"'")


def faults_for(station: OsceStation) -> list[str]:
    """Every reason this station's marks cannot currently be earned.

    A thin wrapper now. The judgement lives in `app.services.osce.sittability`
    so that this script, the admin preview and the sourcing selection cannot
    drift apart - which they had, and that drift is why the audit kept
    reporting stations as fine while a candidate met them broken.
    """
    return [fault.detail for fault in station_faults(station)]


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
