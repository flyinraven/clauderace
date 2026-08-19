"""The queue for reviewing every never-sat station by eye, and the record of it.

397 stations cannot be looked at in one sitting, and a review that loses its
place has to start again. This writes the whole list once, worst first, and
keeps the verdict for each one so the next session resumes instead of
repeating.

Order is by how likely the screen is wrong, not by station number: no view of
the patient first, then stock stand-ins, then everything else.

    python scripts/review_worklist.py                 # build or show progress
    python scripts/review_worklist.py --next 8        # the next ones to open
    python scripts/review_worklist.py --done 90 --note "cornea close-up only"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))
STATE = Path(__file__).resolve().parents[1] / "station_review.json"


def _load_env() -> str:
    for line in (BACKEND / ".env.production").read_text(encoding="utf-8-sig").splitlines():
        if re.match(r"^\s*[A-Z_]+\s*=", line):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL is not set")
    return url


def build() -> list[dict]:
    import sqlalchemy as sa
    from sqlalchemy.orm import Session, selectinload

    from app.models import OsceSession, OsceStation
    from app.services.osce.sittability import station_faults

    engine = sa.create_engine(_load_env(), connect_args={"connect_timeout": 30})
    rows = []
    with Session(engine) as db:
        sat = {r[0] for r in db.execute(sa.select(OsceSession.station_id)).all()}
        for station in db.query(OsceStation).options(
                selectinload(OsceStation.figures)).order_by(OsceStation.id).all():
            if station.id in sat or not (station.prompts or []):
                continue
            kinds = sorted({f.kind for f in station_faults(station)})
            pictures = [f for f in station.figures if f.image_id and f.is_approved]
            weight = 0
            if "no_view_of_the_patient" in kinds:
                weight += 10
            if not pictures:
                weight += 6
            weight += 2 * sum(1 for f in pictures
                              if f.verification_status == "representative")
            weight += len(kinds)
            rows.append({
                "station": station.id,
                "period": station.exam_period,
                "subspecialty": station.subspecialty,
                "images": len(pictures),
                "faults": kinds,
                "weight": weight,
                "status": "todo",
                "note": "",
            })
    rows.sort(key=lambda r: (-r["weight"], r["station"]))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--next", type=int, default=0)
    parser.add_argument("--done", type=int)
    parser.add_argument("--note", default="")
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args()

    if args.rebuild or not STATE.exists():
        rows = build()
        STATE.write_text(json.dumps(rows, indent=1), encoding="utf-8")
        print(f"built {len(rows)} stations")
    rows = json.loads(STATE.read_text(encoding="utf-8"))

    if args.done:
        for row in rows:
            if row["station"] == args.done:
                row["status"] = "done"
                row["note"] = args.note
                break
        STATE.write_text(json.dumps(rows, indent=1), encoding="utf-8")

    if args.next:
        for row in [r for r in rows if r["status"] == "todo"][: args.next]:
            print(f"st{row['station']:<4} w{row['weight']:<3} {(row['period'] or '?'):<17} "
                  f"{(row['subspecialty'] or '?')[:20]:<20} {row['images']} img  "
                  f"{','.join(row['faults'])[:50]}")
        return 0

    done = sum(1 for r in rows if r["status"] == "done")
    print(f"{done} reviewed, {len(rows) - done} to go, of {len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
