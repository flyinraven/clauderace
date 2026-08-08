"""Ask for a differential before the examiner states the diagnosis.

    python scripts/ask_differentials_before_reveal.py           # report only
    python scripts/ask_differentials_before_reveal.py --apply

Stating the diagnosis mid-station is what the real handouts do - Lisa Cooke is
told she carries the 11778 mutation and then asked what it means - but the
reveal always lands after the candidate has been made to reason across
possibilities. Fifty-six stations instead asked for "your leading diagnosis"
and then announced the answer.

No model is involved: the bank writes that question eight ways and all of them
are the same sentence. The question it replaces is kept on the prompt so a
rewrite can be undone.
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
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    url = _load_env()
    import sqlalchemy as sa
    from sqlalchemy.orm import Session
    from sqlalchemy.orm.attributes import flag_modified

    from app.models import OsceStation
    from app.services.osce.prompts import ask_for_differentials, needs_differential_first

    engine = sa.create_engine(url, connect_args={"connect_timeout": 30})
    changed = 0
    with Session(engine) as db:
        for station in db.query(OsceStation).order_by(OsceStation.id).all():
            prompts = [dict(p) for p in (station.prompts or [])]
            index = needs_differential_first(prompts)
            if index is None or index == 0:
                continue
            target = prompts[index - 1]
            before = str(target.get("text") or "")
            after = ask_for_differentials(before)
            if after == before:
                continue

            changed += 1
            print(f"[{station.id}] {station.subspecialty}")
            print(f"    before: {before}")
            print(f"    after : {after}")
            if args.apply:
                target["differential_added"] = {"original": before}
                target["text"] = after
                station.prompts = prompts
                flag_modified(station, "prompts")
        if args.apply:
            db.commit()

    print()
    print(f"{changed} station(s) reveal the diagnosis without asking for a differential")
    print("Applied." if args.apply else "Dry run - pass --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
