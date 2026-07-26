"""Make every marking key add up to its sub-question's mark allocation.

Key points are scaled to fit the marks available and each is rounded to two
decimals, so the sum can drift a few hundredths (eight marks over three points
gives 2.66 x 3 = 7.98). Marks that do not add up are indefensible to a
candidate, so the largest point absorbs the remainder.

Answers generated after this was fixed at source are already correct; this
repairs rows created before then.

    python scripts/repair_mark_totals.py --database-url "postgresql+psycopg://..."
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

from app.models import ModelAnswerPoint, Question, QuestionPart  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    engine = create_engine(args.database_url)
    fixed = 0

    with Session(engine) as db:
        parts = db.execute(select(QuestionPart)).scalars().all()
        for part in parts:
            points = db.execute(
                select(ModelAnswerPoint)
                .where(ModelAnswerPoint.part_id == part.id)
                .order_by(ModelAnswerPoint.position)
            ).scalars().all()
            if not points:
                continue

            total = round(sum(p.marks for p in points), 4)
            drift = round(float(part.marks) - total, 2)
            if abs(drift) < 0.005:
                continue

            question = db.get(Question, part.question_id)
            target = max(points, key=lambda p: p.marks)
            new_value = round(max(0.0, target.marks + drift), 2)
            print(
                f"  Q{question.id} {str(question.topic)[:38]:38s} part "
                f"{part.label or '-'}: key={total:g} vs {part.marks:g} "
                f"-> point {target.position} {target.marks:g} to {new_value:g}"
            )
            if not args.dry_run:
                target.marks = new_value
            fixed += 1

        if not args.dry_run:
            db.commit()

    print(f"\n{'would fix' if args.dry_run else 'fixed'} {fixed} sub-question(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
