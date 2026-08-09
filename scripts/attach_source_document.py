"""Put a past paper back behind the stations that came out of it.

Two uploads were deleted after their stations had been built. The stations
survived - 18 labelled "2025 Semester 2" and 18 "2026 Semester 1", with
findings, questions and images - but with no `source_document_id` they are cut
off from the report they came from. Nothing can re-derive them: the repair that
gives a station its own photographs works by re-segmenting the source PDF, and
there was none.

This stores the file and links the stations already carrying that exam period
back to it. It does NOT ingest: those stations exist, and building them again
would duplicate every one and pay for the privilege. `status` is set to
completed for the same reason - a document left `uploaded` is picked up by the
ingest queue.

    python scripts/attach_source_document.py --from-env \\
        --file "2026 Semester 1 OSCE.pdf" --exam-period "2026 Semester 1" --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.models import OsceStation, SourceDocument  # noqa: E402
from app.services.ingest.extract import extract_document  # noqa: E402

from audit_station_images import url_from_env  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--database-url")
    group.add_argument("--from-env", action="store_true")
    parser.add_argument("--file", required=True, help="path to the PDF")
    parser.add_argument("--exam-period", required=True, help="the stations to link, e.g. '2026 Semester 1'")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    path = Path(args.file)
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    doc = extract_document(data, path.name, "application/pdf")
    pages = doc.page_count

    engine = create_engine(url_from_env() if args.from_env else args.database_url)
    with Session(engine) as db:
        existing = db.execute(
            select(SourceDocument).where(SourceDocument.sha256 == digest)
        ).scalar_one_or_none()
        stations = list(
            db.execute(
                select(OsceStation).where(OsceStation.exam_period == args.exam_period)
            ).scalars()
        )
        unlinked = [s for s in stations if s.source_document_id is None]
        print(f"{path.name}: {len(data):,} bytes, {pages} pages")
        print(f"  already stored: {'yes, document ' + str(existing.id) if existing else 'no'}")
        print(f"  stations for {args.exam_period!r}: {len(stations)} ({len(unlinked)} unlinked)")

        if args.dry_run:
            print("\n--dry-run: nothing written.")
            return 0

        source = existing
        if source is None:
            source = SourceDocument(
                filename=path.name,
                content_type="application/pdf",
                sha256=digest,
                size_bytes=len(data),
                data=data,
                page_count=pages,
                exam_period=args.exam_period,
                document_kind="osce",
                # Completed, not uploaded: the stations are already built, and
                # the ingest queue takes anything still marked uploaded.
                status="completed",
                status_detail=(
                    f"Stored for {len(unlinked)} station(s) built before the "
                    f"original upload was deleted. Not re-ingested."
                ),
                extracted_text=doc.full_text,
            )
            db.add(source)
            db.flush()

        for station in unlinked:
            station.source_document_id = source.id
        db.commit()
        print(f"\nStored as document {source.id}; linked {len(unlinked)} station(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
