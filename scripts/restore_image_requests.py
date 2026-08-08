"""Put back the image request that reconciliation deleted.

    python scripts/restore_image_requests.py            # report only
    python scripts/restore_image_requests.py --apply

Reconciliation removed `image_wanted` from a question it restated as "what
would you expect", to stop later runs paying to search for an image already
known not to exist. That was one fact expressed by destroying another: the
binder matches a question's request against the figures the station already
holds, and a question with no request can never be matched. Twenty-two
questions lost the chance of being handed a figure from the examiners' own
report.

The code now keeps the request and sets `image_search_exhausted` instead. This
restores it on the questions written before that, from
`reconciled.original_image_wanted`, and sets the flag - so the binder can match
and the searcher still will not spend.

Six of the twenty-two cannot be restored: they were rewritten twice, and the
second pass recorded the already-emptied value as the original. Nothing here
can recover those.

No model, no key.
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

    engine = sa.create_engine(url, connect_args={"connect_timeout": 30})
    restored = lost = already = 0
    with Session(engine) as db:
        for station in db.query(OsceStation).order_by(OsceStation.id).all():
            prompts = [dict(p) for p in (station.prompts or [])]
            touched = False
            for prompt in prompts:
                record = prompt.get("reconciled") or {}
                if record.get("mode") != "state":
                    continue
                if prompt.get("image_wanted"):
                    already += 1
                    continue
                original = record.get("original_image_wanted")
                if not original:
                    lost += 1
                    print(f"[{station.id} {prompt.get('label')}] request lost to a second rewrite")
                    continue
                restored += 1
                touched = True
                prompt["image_wanted"] = original
                prompt["image_search_exhausted"] = True
                print(f"[{station.id} {prompt.get('label')}] restored: {original[:64]}")
            if touched and args.apply:
                station.prompts = prompts
                flag_modified(station, "prompts")
        if args.apply:
            db.commit()

    print()
    print(f"requests restored : {restored}")
    print(f"already present   : {already}")
    print(f"unrecoverable     : {lost}")
    print("Applied." if args.apply else "Dry run - pass --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
