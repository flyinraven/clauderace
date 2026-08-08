"""Queue the findings-in-words pass for figures that have no image.

The last resort of the image protocol: sourcing found nothing usable, so the
examiner states the findings aloud instead. This queues one job row, which the
server's worker drains a figure at a time, one model call each. It spends no
image-search quota - it does no searching at all.

Figures that already have words are skipped, so a run can be repeated after a
sourcing round without paying twice.

    python scripts/queue_describe_missing.py --from-env --dry-run
    python scripts/queue_describe_missing.py --from-env --station-ids 119
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.models import Job, OsceFigure  # noqa: E402
from app.models.ops import JOB_PENDING  # noqa: E402
from app.services.osce.station_images import (  # noqa: E402
    JOB_DESCRIBE_STATION_FIGURES,
    figures_needing_description,
)

from audit_station_images import url_from_env  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--database-url")
    group.add_argument("--from-env", action="store_true")
    parser.add_argument(
        "--station-ids", type=int, nargs="+",
        help="only the figures of these stations, so a first batch can be read "
             "before the rest is paid for",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print what would be queued and queue nothing",
    )
    args = parser.parse_args()

    engine = create_engine(url_from_env() if args.from_env else args.database_url)

    with Session(engine) as db:
        # The server's own selection, never a second opinion: the two
        # disagreeing means this queues figures the pass will skip, and reports
        # work that was never done.
        ids = figures_needing_description(db)
        if args.station_ids:
            wanted = set(args.station_ids)
            ids = [
                f.id
                for f in db.execute(
                    select(OsceFigure).where(OsceFigure.id.in_(ids))
                ).scalars()
                if f.station_id in wanted
            ]
        if not ids:
            print("Nothing to describe; every figure without an image has its findings stated.")
            return 0

        stations = sorted({
            f.station_id
            for f in db.execute(select(OsceFigure).where(OsceFigure.id.in_(ids))).scalars()
        })
        print(f"{len(ids)} figure(s) across {len(stations)} station(s): "
              f"{' '.join(str(i) for i in stations)}")
        print("One model call each; no image search.")
        if args.dry_run:
            print("\n--dry-run: nothing queued.")
            return 0

        job = Job(
            job_type=JOB_DESCRIBE_STATION_FIGURES,
            status=JOB_PENDING,
            payload={"figure_ids": sorted(ids)},
            cursor={},
            total_steps=len(ids),
            message=f"Describing {len(ids)} view(s) with no image",
        )
        db.add(job)
        db.commit()
        db.refresh(job)

    print(f"\nQueued job {job.id}. The server drains it one figure at a time.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
