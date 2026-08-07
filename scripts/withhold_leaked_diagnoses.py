"""Move a leaked diagnosis out of the stem on stations already split.

    python scripts/withhold_leaked_diagnoses.py            # report only
    python scripts/withhold_leaked_diagnoses.py --apply

Station 156 opened a real circuit with "The patient presents with bilateral
Brown's Syndrome" printed beside the visual acuity. The split that produced it
has since been fixed, but a rule change only applies where it is written, so
stations split under the old prompt keep what it produced - the same reason
`settle_stations` exists.

No model is involved. `withhold_diagnosis` is deterministic, so this repairs
the stored split without a re-split and without spending anything. The
withheld line is not discarded: it is a real finding, moved to `elicited`,
which is never shown before the result.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))


def _load_env() -> str:
    env_file = BACKEND / ".env.production"
    if not env_file.exists():
        raise SystemExit(f"No env file at {env_file}")
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if re.match(r"^\s*[A-Z_]+\s*=", line):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL is not set")
    return url


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write the changes")
    args = parser.parse_args()

    url = _load_env()
    import sqlalchemy as sa
    from sqlalchemy.orm import Session

    from app.models import OsceStation
    from app.services.osce.findings import withhold_diagnosis

    engine = sa.create_engine(url, connect_args={"connect_timeout": 30})
    changed = 0
    with Session(engine) as db:
        for station in db.query(OsceStation).order_by(OsceStation.id).all():
            given = station.findings_given or ""
            if not given.strip():
                continue
            kept, moved = withhold_diagnosis(given, station)
            if not moved:
                continue
            changed += 1
            print(f"[{station.id}] {station.subspecialty} - {station.diagnosis}")
            for line in moved:
                print(f"    withheld: {line}")
            if args.apply:
                station.findings_given = kept or None
                station.findings_elicited = "\n".join(
                    filter(None, [station.findings_elicited or "", *moved])
                ).strip() or None
        if args.apply:
            db.commit()

    print()
    print(f"{changed} station(s) leak the diagnosis in the stem")
    print("Applied." if args.apply else "Dry run - pass --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
