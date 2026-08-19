"""Stop asking for both eyes when only one is on screen.

Twelve never-sat stations open "Please examine ... of both eyes" with every
image of a single side. The candidate cannot describe an eye they were not
shown, so the instruction costs them attention and confidence for nothing.

Nine of the twelve have NOT ONE rubric item that needs the second eye: the
marks are all earnable from the eye that is there. On those the question is
narrowed to the side actually shown, which is honest about what is on screen
and changes no mark.

Three are left exactly as they are, because their marks really do depend on
comparing the eyes, and narrowing the question would make an earnable mark
unearnable - the opposite of the repair:

  st317  3.5m  "Recognise bilateral optic disc swelling"          (IIH)
  st177  4.0m  pupil asymmetry, and right CDR 0.8 against left 0.2
  st622  2.0m  "Identifies bilateral peripheral iridotomies"

Those need the other eye's image, which means sourcing, which is off.

Not touched either: st616, where the checker is wrong rather than the station.
Its question reads "This young lady has reduced vision of 6/36 in both eyes.
Please examine the right fundus" - it asks for one eye and says so. `_BOTH_RE`
matched the acuity preamble. Fixed separately in sittability.

Only stations never sat. No model, no search, nothing spent.

    python scripts/ask_for_the_eye_that_is_shown.py            # report
    python scripts/ask_for_the_eye_that_is_shown.py --apply
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

# Marks that only exist because there are two eyes to compare.
NEEDS_BOTH = re.compile(r"\bboth\b|\bbilateral|\beach eye\b|\bOU\b|asymmetr|compare", re.I)
# Keep the preposition. Substituting the whole phrase produced "Please examine
# the orbits the left eye".
BOTH_EYES = re.compile(r"\b(of|in)?\s*both eyes\b", re.I)
# A question that already names one side as the thing to examine is not asking
# for both, whatever a preamble says. st616 reads "reduced vision of 6/36 in
# both eyes. Please examine the right fundus" - rewriting it broke the acuity.
ALREADY_ONE_SIDE = re.compile(r"\bexamine[^.?!]*\b(left|right)\b", re.I)


def _load_env() -> str:
    for line in (BACKEND / ".env.production").read_text(encoding="utf-8-sig").splitlines():
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

    import sqlalchemy as sa
    from sqlalchemy.orm import Session, selectinload
    from sqlalchemy.orm.attributes import flag_modified

    from app.models import OsceSession, OsceStation
    from app.services.osce.sittability import station_faults

    engine = sa.create_engine(_load_env(), connect_args={"connect_timeout": 30})
    narrowed = kept = 0
    with Session(engine) as db:
        sat = {r[0] for r in db.execute(sa.select(OsceSession.station_id)).all()}
        for station in db.query(OsceStation).options(
                selectinload(OsceStation.figures)).order_by(OsceStation.id).all():
            if station.id in sat:
                continue
            faults = [f for f in station_faults(station) if f.kind == "missing_side"]
            if not faults:
                continue

            # Which side the station actually shows, from the captions.
            sides = set()
            for figure in station.figures:
                if not (figure.image_id and figure.is_approved):
                    continue
                caption = (figure.caption or "").lower()
                if re.search(r"\bleft\b", caption):
                    sides.add("left")
                if re.search(r"\bright\b", caption):
                    sides.add("right")
            if len(sides) != 1:
                print(f"-- st{station.id}: captions name {sides or 'no side'}, left alone")
                kept += 1
                continue
            side = sides.pop()

            for prompt in station.prompts or []:
                label = str(prompt.get("label") or "?")
                if not any(f"question {label} " in f.detail for f in faults):
                    continue
                rubric = prompt.get("rubric") or []
                needs = [r for r in rubric if NEEDS_BOTH.search(str(r.get("text") or ""))]
                if needs:
                    marks = sum(float(r.get("marks") or 0) for r in needs)
                    print(f"-- st{station.id} {label}: {marks:g} marks compare the eyes, "
                          f"left alone ({needs[0].get('text')!r})")
                    kept += 1
                    continue
                text = str(prompt.get("text") or "")
                if ALREADY_ONE_SIDE.search(text):
                    print(f"-- st{station.id} {label}: already asks for one side, "
                          f"left alone ({text[:60]!r})")
                    kept += 1
                    continue
                new = BOTH_EYES.sub(
                    lambda m: f"{m.group(1) + ' ' if m.group(1) else ''}the {side} eye",
                    text)
                if new == text:
                    print(f"-- st{station.id} {label}: no 'both eyes' to narrow, left alone")
                    kept += 1
                    continue
                print(f"st{station.id} {label} (shows {side})\n  - {text}\n  + {new}\n")
                prompt["text"] = new
                flag_modified(station, "prompts")
                narrowed += 1

        print(f"\n{narrowed} question(s) narrowed to the eye on screen, {kept} left alone")
        if args.apply:
            db.commit()
            print("applied")
        else:
            db.rollback()
            print("re-run with --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
