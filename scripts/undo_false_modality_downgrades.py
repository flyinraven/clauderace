"""Undo the modality downgrades the first blind sweep got wrong.

    python scripts/undo_false_modality_downgrades.py            # report only
    python scripts/undo_false_modality_downgrades.py --apply

The sweep compared each image's observed modality against
`expected_modalities_for`, which for a figure that named no particular view
guesses from the station's whole findings blob: "anterior segment and an optic
nerve pigmented lesion" yields external/slit_lamp/topography and calls the
correct fundus photograph wrong. 146 of 169 disagreements were that.

The rule is fixed. This repairs what it already wrote, with no vision calls.

`match_confidence` is set to NULL rather than to a number. The original score
was overwritten by the cap and is genuinely unknown now; NULL is the state this
codebase already has for that - `opening_image_is_settled` reads it as "from
before the score was recorded, not a bad one" and leaves the figure alone,
which is the outcome that stops a re-source re-buying an image that was right.

Laterality disagreements are left exactly as they are. That arm was never in
doubt: the findings text really does say whether both eyes are involved.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

MARKER = "  [Looked at without the station: "
MODALITY_NOTE = re.compile(r"this is a \w+ image; the question asks for")
# What the sweep wrote when it downgraded: min(existing, MIN_REPRESENTATIVE).
CAPPED = 0.55


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

    from app.models import OsceFigure

    engine = sa.create_engine(url, connect_args={"connect_timeout": 30})
    cleared = restored = kept = 0
    with Session(engine) as db:
        figures = (
            db.query(OsceFigure)
            .filter(OsceFigure.verification_notes.contains("Looked at without the station"))
            .order_by(OsceFigure.id)
            .all()
        )
        for figure in figures:
            notes = figure.verification_notes or ""
            head, _, tail = notes.partition(MARKER)
            if not MODALITY_NOTE.search(tail):
                kept += 1          # a laterality disagreement: correct, left alone
                continue
            if (figure.wanted_description or "").strip():
                kept += 1          # a real request for a named view: still checked
                continue

            cleared += 1
            figure.verification_notes = head.rstrip() or None
            # Only a figure the sweep downgraded carries the cap exactly; one
            # that was already representative kept its own score and its tier.
            was_downgraded = (
                figure.verification_status == "representative"
                and figure.match_confidence is not None
                and abs(figure.match_confidence - CAPPED) < 1e-6
            )
            if was_downgraded:
                restored += 1
                figure.verification_status = "faithful"
                figure.match_confidence = None
        if args.apply:
            db.commit()

    print(f"figures carrying a blind-sweep note : {len(figures)}")
    print(f"  false modality notes cleared      : {cleared}")
    print(f"    of which tier restored to faithful: {restored}")
    print(f"  left as they are (laterality, or a real request): {kept}")
    print("Applied." if args.apply else "Dry run - pass --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
