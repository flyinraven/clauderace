"""Queue an image re-source for the stations the audit says are unanswerable.

`audit_station_images.py` says which stations cannot be marked from the images
they have. This turns that list into work: one job row per run, which the
server's job worker picks up and drains a station at a time.

The selection is the audit's, not a second opinion - the two must agree or the
run spends on stations the audit will still flag afterwards. What this adds is
batching and a cap, because sourcing the whole bank is a long series of Brave
and vision calls on one free-tier instance.

Sourcing leaves a station's own image alone when it is already a confident,
approved, still-current match, so a station in the list only because question C
wants an MRI costs one search rather than three. Pass --resource-everything to
re-buy those too.

    python scripts/queue_station_images.py --from-env --dry-run
    python scripts/queue_station_images.py --from-env --limit 10
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

from app.models import Job, OsceStation  # noqa: E402
from app.models.ops import JOB_PENDING  # noqa: E402
from app.services.osce.station_images import (  # noqa: E402
    JOB_SOURCE_STATION_IMAGES,
    opening_image_is_settled,
)

from audit_station_images import faults_for, url_from_env  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--database-url")
    group.add_argument("--from-env", action="store_true")
    parser.add_argument(
        "--limit", type=int,
        help="queue at most this many stations, so the results can be read "
             "before the next batch is paid for",
    )
    parser.add_argument(
        "--station-ids", type=int, nargs="+",
        help="queue these stations instead of the audit's list",
    )
    parser.add_argument(
        "--resource-everything", action="store_true",
        help="also re-source opening images that are already good",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print what would be queued and queue nothing",
    )
    args = parser.parse_args()

    engine = create_engine(url_from_env() if args.from_env else args.database_url)
    only_missing = not args.resource_everything

    with Session(engine) as db:
        if args.station_ids:
            stations = db.execute(
                select(OsceStation)
                .where(OsceStation.id.in_(args.station_ids))
                .order_by(OsceStation.id)
            ).scalars().all()
        else:
            stations = [
                s for s in db.execute(
                    select(OsceStation).order_by(OsceStation.id)
                ).scalars().all()
                if faults_for(s)
            ]

        if args.limit:
            stations = stations[: args.limit]
        if not stations:
            print("Nothing to source; every station can be answered from its images.")
            return 0

        settled = sum(1 for s in stations if only_missing and opening_image_is_settled(s))
        ids = [s.id for s in stations]

        print(f"{len(ids)} station(s): {' '.join(str(i) for i in ids)}")
        print(
            f"{settled} keep the image they have and pay only for what is missing; "
            f"{len(ids) - settled} have their own image searched again."
        )
        if args.dry_run:
            print("\n--dry-run: nothing queued.")
            return 0

        job = Job(
            job_type=JOB_SOURCE_STATION_IMAGES,
            status=JOB_PENDING,
            payload={"station_ids": ids, "only_missing": only_missing},
            cursor={},
            total_steps=len(ids),
            message=f"Sourcing images for {len(ids)} station(s)",
        )
        db.add(job)
        db.commit()
        db.refresh(job)

    print(f"\nQueued job {job.id}. The server drains it one station at a time.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
