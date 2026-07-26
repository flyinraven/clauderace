"""Copy the question bank from local SQLite into the production database.

Everything in the bank cost real money to produce - the model answers alone
were 39 Sonnet calls - so a deployment must carry it across rather than
regenerate it.

Primary keys are preserved so that cross-table references stay intact, and the
PostgreSQL identity sequences are reset afterwards; without that the first
insert on the live site would collide with a migrated row.

Sittings, jobs, AI call logs and the error log are deliberately NOT copied:
they are local test noise.

Usage (from the repo root, with the backend venv active):

    python scripts/migrate_to_production.py \
        --target "postgresql+psycopg://user:pass@host:5432/dbname"

Add --include-documents to copy the original uploaded PDFs as well; they are
only needed if you intend to re-ingest, and they are large.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, func, insert, inspect, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.models import Base  # noqa: E402

# Order matters: parents before children.
TABLE_ORDER = [
    "users",
    "settings",
    "curriculum_standards",
    "images",
    "source_documents",
    "questions",
    "question_parts",
    "model_answer_points",
    "examiner_feedback",
    "figures",
    "osce_stations",
    "osce_figures",
    "exam_papers",
    "exam_paper_questions",
]

# Local-only operational data.
SKIP_ALWAYS = {
    "jobs", "ai_calls", "error_log", "alembic_version",
    "exam_sessions", "answers", "grades", "session_results",
    "osce_sessions", "osce_responses", "osce_grades", "osce_results",
    "osce_circuits", "audio_clips", "invites",
}


def copy_table(src: Session, dst: Session, table, batch: int = 200) -> int:
    rows = src.execute(select(table)).mappings().all()
    if not rows:
        return 0
    payload = [dict(r) for r in rows]
    for start in range(0, len(payload), batch):
        dst.execute(insert(table), payload[start : start + batch])
    dst.commit()
    return len(payload)


def reset_sequences(dst: Session, table) -> None:
    """Point PostgreSQL's identity sequence past the migrated rows."""
    if dst.bind.dialect.name != "postgresql":
        return
    pk = list(table.primary_key.columns)
    if len(pk) != 1 or pk[0].name != "id":
        return
    dst.execute(
        text(
            "SELECT setval(pg_get_serial_sequence(:t, 'id'), "
            "COALESCE((SELECT MAX(id) FROM " + table.name + "), 1), true)"
        ),
        {"t": table.name},
    )
    dst.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="sqlite:///backend/race.db")
    parser.add_argument("--target", required=True, help="Production DATABASE_URL")
    parser.add_argument("--include-documents", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    src_engine = create_engine(args.source)
    dst_engine = create_engine(args.target)

    tables = {t.name: t for t in Base.metadata.sorted_tables}

    with Session(src_engine) as src, Session(dst_engine) as dst:
        print(f"source : {args.source}")
        print(f"target : {args.target.split('@')[-1]}\n")

        # The API creates its own schema on first boot, so an empty target is
        # the normal state before deployment - say so rather than dying on a
        # "no such table" traceback.
        if not inspect(dst_engine).has_table("questions"):
            print("ABORT: the target database has no tables yet.")
            print("Deploy the API to Render first and let it boot once - it runs")
            print("its migrations on startup - then run this again.")
            return 1

        # Refuse to run into a database that already holds content, rather
        # than silently duplicating a bank that took real money to build.
        existing = dst.execute(select(func.count()).select_from(tables["questions"])).scalar_one()
        if existing and not args.dry_run:
            print(f"ABORT: the target already has {existing} question(s).")
            print("Migrating again would duplicate them. Clear it first if that is intended.")
            return 1

        total = 0
        for name in TABLE_ORDER:
            if name in SKIP_ALWAYS:
                continue
            if name == "source_documents" and not args.include_documents:
                print(f"  {name:24s} skipped (use --include-documents)")
                continue
            table = tables.get(name)
            if table is None:
                continue

            count = src.execute(select(func.count()).select_from(table)).scalar_one()
            if args.dry_run:
                print(f"  {name:24s} would copy {count}")
                continue

            copied = copy_table(src, dst, table)
            reset_sequences(dst, table)
            total += copied
            print(f"  {name:24s} copied {copied}")

        if not args.dry_run:
            print(f"\n{total} row(s) migrated.")
            print(
                "\nReminder: SETTINGS_ENCRYPTION_KEY on Render must match the local "
                "one, or the migrated API keys will be unreadable."
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
