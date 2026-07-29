"""Fold typesetter glyphs out of text that was already ingested.

The RANZCO PDFs carry ligature glyphs in their text layer, so "a useful
negative finding" was stored as "a useful negative ﬁnding" (U+FB01). It
renders acceptably, which is why it went unnoticed for so long, and then no
search for "finding" matches it and the grading model is handed a word it has
to guess at. Smart quotes and non-breaking spaces arrive the same way.

Extraction now normalises on the way in (`ingest.extract.normalise_extracted_text`).
This repairs rows written before that, without re-ingesting: re-ingestion would
re-run the structuring model over every document and bill for it, and would
also discard the model answers, figures and examiner feedback hanging off the
existing questions.

Every text-bearing column that can hold document-derived prose is swept,
including the JSON ones - a station's `prompts` and `rubric` are where most of
the OSCE text actually lives.

    python scripts/normalise_ingested_text.py --database-url "postgresql+psycopg://..." --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.orm.attributes import flag_modified  # noqa: E402

from app.models import (  # noqa: E402
    ExaminerFeedback,
    Figure,
    ModelAnswerPoint,
    OsceFigure,
    OsceStation,
    Question,
    QuestionPart,
    SourceDocument,
)
from app.services.ingest.extract import normalise_extracted_text  # noqa: E402

# Plain string columns, and JSON columns walked to their leaves. Only fields
# that can carry prose lifted from a document: codes, statuses, filenames and
# enum-ish columns are deliberately absent.
TEXT_COLUMNS: dict[Any, tuple[str, ...]] = {
    Question: ("topic", "purpose", "stem", "curriculum_standard_raw", "angoff_rationale"),
    QuestionPart: ("text", "preamble"),
    ModelAnswerPoint: ("text", "rationale"),
    Figure: ("label", "caption", "wanted_description"),
    OsceStation: (
        "title", "case_summary", "patient_history", "patient_demographic",
        "findings", "diagnosis", "findings_given", "findings_elicited",
        "cohort_performance",
    ),
    OsceFigure: ("caption", "wanted_description", "search_query", "verification_notes"),
    SourceDocument: ("extracted_text",),
}
JSON_COLUMNS: dict[Any, tuple[str, ...]] = {
    ExaminerFeedback: ("common_mistakes", "cohort_impression"),
    OsceStation: ("tasks", "rubric", "prompts"),
}


def normalise_deep(value: Any) -> tuple[Any, int]:
    """Normalise every string inside a JSON structure. Returns (value, changes)."""
    if isinstance(value, str):
        cleaned = normalise_extracted_text(value)
        return cleaned, int(cleaned != value)
    if isinstance(value, list):
        changes = 0
        out = []
        for item in value:
            new_item, n = normalise_deep(item)
            out.append(new_item)
            changes += n
        return out, changes
    if isinstance(value, dict):
        changes = 0
        out = {}
        for key, item in value.items():
            new_item, n = normalise_deep(item)
            out[key] = new_item
            changes += n
        return out, changes
    return value, 0


def _sample(before: str, after: str) -> str:
    """The first word that actually changed, for the log line."""
    for old, new in zip(before.split(), after.split()):
        if old != new:
            return f"{old!r} -> {new!r}"
    return f"{before[:30]!r} -> {after[:30]!r}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--verbose", action="store_true", help="print every field that changed"
    )
    args = parser.parse_args()

    engine = create_engine(args.database_url)
    per_table: dict[str, int] = {}
    rows_touched = 0

    with Session(engine) as db:
        models = sorted(
            set(TEXT_COLUMNS) | set(JSON_COLUMNS), key=lambda m: m.__tablename__
        )
        for model in models:
            table = model.__tablename__
            for row in db.execute(select(model)).scalars().all():
                changes = 0

                for field in TEXT_COLUMNS.get(model, ()):
                    before = getattr(row, field)
                    if not before:
                        continue
                    after = normalise_extracted_text(before)
                    if after == before:
                        continue
                    if args.verbose:
                        print(f"  {table}#{row.id}.{field}: {_sample(before, after)}")
                    if not args.dry_run:
                        setattr(row, field, after)
                    changes += 1

                for field in JSON_COLUMNS.get(model, ()):
                    before = getattr(row, field)
                    if not before:
                        continue
                    after, n = normalise_deep(before)
                    if not n:
                        continue
                    if args.verbose:
                        print(f"  {table}#{row.id}.{field}: {n} string(s)")
                    if not args.dry_run:
                        # `normalise_deep` rebuilds rather than mutates, so the
                        # assignment differs from the loaded value and would
                        # persist on its own. flag_modified says so explicitly:
                        # a JSON column edited in place compares equal to what
                        # SQLAlchemy loaded and is silently never written.
                        setattr(row, field, after)
                        flag_modified(row, field)
                    changes += 1

                if changes:
                    per_table[table] = per_table.get(table, 0) + 1
                    rows_touched += 1

        if not args.dry_run:
            db.commit()

    verb = "would clean" if args.dry_run else "cleaned"
    if not rows_touched:
        print("Nothing to do: no ligatures or smart punctuation found.")
        return 0
    print(f"\n{verb} {rows_touched} row(s):")
    for table, count in sorted(per_table.items(), key=lambda kv: -kv[1]):
        print(f"  {count:>5}  {table}")
    if args.dry_run:
        print("\nRe-run without --dry-run to apply. Use --verbose to see each change.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
