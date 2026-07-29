"""Report past-paper content whose source document is missing. Read-only.

Only `past_paper` rows can be orphaned. Generated questions and stations were
invented by the model and never had a document, so a NULL source there is
correct and is reported separately rather than counted as damage.

Two things detach a past-paper row from its document, and they are not equally
alarming:

  - `migrate_to_production.py` blanks source_document_id on every row unless
    it is given --include-documents, because the PDFs are large and are only
    needed to re-ingest. This is deliberate, and the documents are still in
    the local database it copied from.
  - Deleting a document used to leave its OSCE stations behind with a dangling
    source, because the delete guard only counted questions and an OSCE report
    therefore looked like nothing depended on it (fixed in `api.documents`).

Either way the content works; what is gone is the audit trail back to the PDF.
Check the local database before assuming the second: if the documents are
sitting there, it was the migration.

    python scripts/check_orphaned_content.py --database-url "postgresql+psycopg://..."

Or, reading the URL out of backend/.env.production rather than passing it:

    python scripts/check_orphaned_content.py --from-env
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.models import OsceStation, Question, SourceDocument  # noqa: E402


def url_from_env() -> str:
    env = (BACKEND / ".env.production")
    if not env.exists():
        raise SystemExit(f"{env} not found; pass --database-url instead")
    match = re.search(
        r"^DATABASE_URL\s*=\s*(.+)$", env.read_text(encoding="utf-8-sig"), re.M
    )
    if not match:
        raise SystemExit("No DATABASE_URL in backend/.env.production")
    return match.group(1).strip().strip("\"'")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--database-url")
    group.add_argument("--from-env", action="store_true")
    args = parser.parse_args()

    url = url_from_env() if args.from_env else args.database_url
    # Never print the URL: it carries the password.
    engine = create_engine(url)
    orphans = 0

    with Session(engine) as db:
        print("Documents still held:")
        docs = db.execute(
            select(SourceDocument).order_by(SourceDocument.id)
        ).scalars().all()
        for doc in docs:
            print(
                f"  #{doc.id} {doc.filename[:44]:44s} {str(doc.exam_period):<18}"
                f"{str(doc.document_kind):<8}{doc.status}"
            )
        if not docs:
            print("  (none)")

        for model, noun in ((OsceStation, "station"), (Question, "question")):
            print(f"\n{noun.title()}s by sitting:")
            rows = db.execute(
                select(
                    model.exam_period,
                    model.source,
                    func.count(model.id),
                    func.count(model.source_document_id),
                ).group_by(model.exam_period, model.source)
                .order_by(model.exam_period, model.source)
            ).all()
            print(f"  {'sitting':<20}{'source':<14}{'total':>7}{'w/ document':>13}")
            for period, source, total, with_doc in rows:
                missing = total - with_doc
                # Generated content never had a document. Saying so keeps the
                # count honest: only past-paper rows can actually be orphaned.
                if source != "past_paper":
                    note = "   (generated - no document expected)"
                elif missing:
                    note = f"   <-- {missing} orphaned"
                    orphans += missing
                else:
                    note = ""
                print(
                    f"  {str(period):<20}{str(source):<14}{total:>7}{with_doc:>13}{note}"
                )

    print()
    if orphans:
        print(f"{orphans} past-paper row(s) have no source document.")
        print(
            "Check backend/race.db for those documents before concluding they "
            "were deleted: migrate_to_production.py blanks this link unless run "
            "with --include-documents."
        )
    else:
        print("Every past-paper row still points at the document it came from.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
