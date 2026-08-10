"""Write back a station its own paper's ingest dropped.

An ingest that fails one block records it in a field nobody reads - "Created
17 item(s). Failed: OSCE 6A." - and carries on. Recovering it by re-ingesting
the document is the obvious move and the wrong one: the ingest clears
everything it previously created, so the seventeen siblings that have since
been graded, furnished with images and described would all be thrown away and
paid for again.

This queues a job that structures the one missing block and persists it beside
the others, then chains the same four follow-on steps in the same order a
normal ingest would: findings split, prompt build, figure check, image
sourcing. A recovered station assembled by hand is a station assembled
differently, and the point of the bank is that the candidate cannot tell which
stations were awkward.

The work happens on the server because the provider keys live encrypted in the
settings table, readable only by the process holding SETTINGS_ENCRYPTION_KEY.
This script reads the paper, says what is missing, and queues.

    python scripts/recover_dropped_station.py --from-env --document 16 --list
    python scripts/recover_dropped_station.py --from-env --document 16 --block "OSCE 6A"
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

from app.models import Job, OsceStation, SourceDocument  # noqa: E402
from app.models.ops import JOB_PENDING  # noqa: E402
from app.services.ingest.pipeline import (  # noqa: E402
    JOB_RECOVER_DROPPED_STATION,
    _load_blocks,
)

from audit_station_images import url_from_env  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--database-url")
    group.add_argument("--from-env", action="store_true")
    parser.add_argument("--document", type=int, required=True)
    parser.add_argument("--block", help='the label the ingest reported, e.g. "OSCE 6A"')
    parser.add_argument("--list", action="store_true",
                        help="show the document's blocks and which are already in the bank")
    args = parser.parse_args()

    if not args.list and not args.block:
        parser.error("pass --block, or --list to see what there is")

    engine = create_engine(url_from_env() if args.from_env else args.database_url)

    with Session(engine) as db:
        source = db.get(SourceDocument, args.document)
        if source is None:
            print(f"No source document {args.document}.")
            return 1

        _, kind, blocks = _load_blocks(db, source)
        if kind != "osce":
            print(f"Document {args.document} is a {kind} paper, not an OSCE.")
            return 1

        existing = {
            (s.station_label or str(s.station_number))
            for s in db.execute(
                select(OsceStation).where(OsceStation.source_document_id == source.id)
            ).scalars()
        }

        if args.list:
            print(f"{source.filename}: {len(blocks)} block(s)")
            for block in blocks:
                printed = block.printed_number or str(block.number)
                mark = "in the bank" if printed in existing else "MISSING"
                print(f"  {block.label:<12} printed {printed:<4} {mark}")
            return 0

        wanted = [b for b in blocks if b.label == args.block]
        if not wanted:
            print(f"No block labelled {args.block!r}. Run --list to see them.")
            return 1
        block = wanted[0]

        printed = block.printed_number or str(block.number)
        if printed in existing:
            # Writing it twice is worse than leaving it missing: the candidate
            # meets the same station under two numbers and neither is wrong.
            print(f"Station {printed} of this document is already in the bank. "
                  "Nothing to recover.")
            return 0

        job = Job(
            job_type=JOB_RECOVER_DROPPED_STATION,
            status=JOB_PENDING,
            payload={"document_id": source.id, "block_label": block.label},
            cursor={},
            total_steps=1,
            message=f"Recovering {block.label} of {source.filename}",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id

    print(f"Queued job {job_id} to recover {args.block} "
          f"({len(block.text)} characters of extract).")
    print("It chains the findings split, prompt build, figure check and image "
          "sourcing behind it, exactly as an ingest would.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
