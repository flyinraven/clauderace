"""Derive the one-line patient demographic shown at the start of a station.

A station used to open with the full history, which names or strongly implies
the diagnosis - "old photographs show a right head tilt since childhood" gives
away a congenital superior oblique palsy before the candidate has looked at the
patient. All they should see is who is sitting in front of them.

Stations are processed in batches to keep this to a few cents.

    python scripts/backfill_demographics.py --database-url "postgresql+psycopg://..."
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

from app.models import OsceStation  # noqa: E402
from app.services.ai import AIClient  # noqa: E402

BATCH = 12

SYSTEM = """\
For each OSCE station you are given, write the single line a candidate sees
before the station begins: the patient's age band and sex, and nothing else.

Age bands: "A child", "A young boy", "A young girl", "A teenage boy",
"A teenage girl", "A young man", "A young woman", "A middle-aged man",
"A middle-aged woman", "An elderly man", "An elderly woman".

It must give away nothing else. No symptoms, no diagnosis, no history, no
mention of surgery, spectacles, head posture or any visible abnormality - the
candidate is meant to find those. "An elderly woman" is right; "An elderly
woman with a head tilt" is wrong.

Return ONLY a JSON object mapping each id, as a string, to its line:
{"12": "An elderly woman", "13": "A young boy"}"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--force", action="store_true", help="Redo ones already set")
    args = parser.parse_args()

    engine = create_engine(args.database_url)
    done = 0

    with Session(engine) as db:
        stmt = select(OsceStation).order_by(OsceStation.id)
        if not args.force:
            stmt = stmt.where(OsceStation.patient_demographic.is_(None))
        stations = db.execute(stmt).scalars().all()
        print(f"{len(stations)} station(s) to describe")
        if not stations:
            return 0

        client = AIClient(db)
        for start in range(0, len(stations), BATCH):
            chunk = stations[start : start + BATCH]
            lines = [
                f'id {s.id}: {(s.patient_history or s.case_summary or "unknown")[:300]}'
                for s in chunk
            ]
            try:
                data = client.complete_json(
                    task="utility", system=SYSTEM, user="\n\n".join(lines),
                    max_tokens=1200,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  batch at {start} failed: {str(exc)[:120]}")
                continue

            if not isinstance(data, dict):
                print(f"  batch at {start}: unexpected shape")
                continue

            for station in chunk:
                value = data.get(str(station.id)) or data.get(station.id)
                if isinstance(value, str) and value.strip():
                    station.patient_demographic = value.strip()[:120]
                    done += 1
            db.commit()
            print(f"  {min(start + BATCH, len(stations))}/{len(stations)}", flush=True)

        print(f"\nset {done} demographic line(s)")
        missing = db.execute(
            select(OsceStation.id).where(OsceStation.patient_demographic.is_(None))
        ).scalars().all()
        if missing:
            print(f"still unset: {missing}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
