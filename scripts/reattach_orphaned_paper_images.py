"""Give a station back the photographs its own examiners' report printed.

Verification used to be a gate, and it rejected the report for looking like a
report: a Humphrey printout is text and numbers by nature, an OCT report
carries measurement overlays, and 118 real CTs, fields, OCTs and fundus
photographs were dropped in one pass on that basis. The gate is gone - an image
printed in the examiners' report is one the real candidates were shown, and it
goes live - but the figures it dropped never came back. Meanwhile 149 stations
open on a web lookalike, and station 155 holds two of its own photographs
unattached while its candidate described a corneal graft in the wrong eye off a
stranger's.

The report itself says which image belongs to which station, so this asks it
rather than guessing. Segmenting the stored PDF gives back the same blocks the
ingest built each station from - the same page ranges, carrying the same images
- and a block knows its printed station number. An earlier version of this
inferred ownership from which page happened to hold a surviving picture, which
placed 58 of the 102 and left 44 with nothing to reason from; the document
knows the answer for all of them.

No model call: extraction and segmentation are deterministic and free.

Restored figures come back as `unverified`, so the classification pass records
what each one is and a question wanting an OCT can be handed this paper's OCT.

    python scripts/reattach_orphaned_paper_images.py --from-env --dry-run
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.models import (  # noqa: E402
    Figure,
    Image,
    OsceFigure,
    OsceStation,
    SourceDocument,
)
from app.services.ingest.extract import extract_document  # noqa: E402
from app.services.ingest.segment import segment  # noqa: E402

from audit_station_images import url_from_env  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--database-url")
    group.add_argument("--from-env", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    engine = create_engine(url_from_env() if args.from_env else args.database_url)
    placed: dict[int, list[tuple[int, str | None]]] = defaultdict(list)
    unmatched: list[str] = []
    attached_total = 0

    with Session(engine) as db:
        # Both kinds of figure. The written papers draw on the same images
        # table, and counting only the OSCE side called 26 of their pictures
        # orphans.
        used = {
            image_id
            for (image_id,) in db.execute(
                select(OsceFigure.image_id).where(OsceFigure.image_id.is_not(None))
            ).all()
        } | {
            image_id
            for (image_id,) in db.execute(
                select(Figure.image_id).where(Figure.image_id.is_not(None))
            ).all()
        }
        by_digest = {
            digest: image_id
            for image_id, digest in db.execute(select(Image.id, Image.sha256)).all()
        }

        for source in db.execute(
            select(SourceDocument).order_by(SourceDocument.id)
        ).scalars():
            if not source.data:
                continue
            doc = extract_document(source.data, source.filename, source.content_type)
            _kind, blocks = segment(doc)
            stations = {}
            for station in db.execute(
                select(OsceStation).where(OsceStation.source_document_id == source.id)
            ).scalars():
                for key in (station.station_label, str(station.station_number or "")):
                    if key:
                        stations.setdefault(key, station)

            found = 0
            for block in blocks:
                station = stations.get(block.printed_number) or stations.get(
                    str(block.number or "")
                )
                if station is None:
                    if block.images:
                        unmatched.append(f"{source.filename} {block.label}")
                    continue
                held = {
                    f.image_id
                    for f in db.execute(
                        select(OsceFigure).where(OsceFigure.station_id == station.id)
                    ).scalars()
                }
                for extracted in block.images:
                    image_id = by_digest.get(extracted.sha256)
                    if image_id is None or image_id in used or image_id in held:
                        continue
                    placed[station.id].append(
                        (image_id, extracted.caption or extracted.label)
                    )
                    held.add(image_id)
                    used.add(image_id)
                    found += 1
            print(f"{source.filename}: {len(blocks)} block(s), {found} image(s) to restore")

        total = sum(len(v) for v in placed.values())
        print(f"\n{total} image(s) the report gives to a station that does not hold them")
        print(f"  across {len(placed)} station(s)")
        if unmatched:
            print(f"  {len(unmatched)} block(s) with images matched no station:")
            for label in unmatched[:6]:
                print(f"      {label}")
        for station_id, items in sorted(placed.items())[:15]:
            print(f"    station {station_id}: {len(items)} image(s)")

        if args.dry_run:
            print("\n--dry-run: nothing attached.")
            return 0

        for station_id, items in sorted(placed.items()):
            highest = max(
                (
                    f.position
                    for f in db.execute(
                        select(OsceFigure).where(OsceFigure.station_id == station_id)
                    ).scalars()
                ),
                default=-1,
            )
            for offset, (image_id, caption) in enumerate(items, start=1):
                db.add(
                    OsceFigure(
                        station_id=station_id,
                        image_id=image_id,
                        position=highest + offset,
                        caption=caption,
                        # Unverified so the classification pass records the
                        # modality; it is approval that no longer waits.
                        verification_status="unverified",
                        is_approved=True,
                    )
                )
                attached_total += 1
        db.commit()

    print(f"\nAttached {attached_total} image(s) to {len(placed)} station(s).")
    print("Run the figure check next so each one's modality is recorded, then")
    print("settling, so a question wanting an OCT is handed this paper's own.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
