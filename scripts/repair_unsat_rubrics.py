"""Give the unearnable questions marks, and drop the items worth nothing.

Two defects that `station_faults` has never looked for, because both are in
the rubric rather than the images:

  - A question carrying no rubric at all. The station's 20 marks are fully
    allocated elsewhere, so it can never score, and the candidate spends 60 to
    120 seconds of a nine-minute station on it and is told afterwards "This
    question carries no marks". Fifteen across the bank.
  - A rubric item whose marks are 0.0, which is the same failure one level
    down: printed in the marking key, impossible to earn.

Only stations never sat are touched. A station already sat has its marks
recorded against the wording that was on screen at the time, and rewriting it
now would make the feedback describe a station that never existed - the reason
`stations_needing_repair` takes a `skip` set. That leaves 4 of the 15 and 11
of the worthless-item stations.

Marks are moved, never invented: each addition below names the sibling item it
is taken from, and the station total is asserted afterwards.

    python scripts/repair_unsat_rubrics.py            # report only
    python scripts/repair_unsat_rubrics.py --apply
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

# (station, label to fund from, fragment of that item, marks to take,
#  label to give to, new item text)
REDISTRIBUTE: list[tuple[int, str, str, float, str, str]] = [
    # "What are the common mistakes or pitfalls you would advise patients with
    # Stargardt's to avoid?" - the pitfalls are already half of C's ocular
    # plan, so the marks follow the question that actually asks for them.
    (84, "C", "Propose an ocular management plan", 1.5, "D",
     "Advises the specific pitfalls to avoid (e.g. vitamin A supplementation, "
     "smoking, excessive light exposure without protection)."),
    # "This is her OCT. Talk me through what it shows." - reading the OCT was
    # being paid for inside E's management item instead.
    (173, "E", "Provides an appropriate management strategy for CNV", 1.5, "C",
     "Describes the OCT appearance, including the macular scarring and whether "
     "there is intraretinal or subretinal fluid to suggest active CNV."),
    # "Given these findings, what is your differential diagnosis for this
    # patient's retinal appearance?" - A was carrying 5 marks for staging
    # alone, which is more than the stage is worth.
    (305, "A", "Accurately stage the ROP", 2.0, "B",
     "Offers a differential for the retinal appearance (e.g. familial "
     "exudative vitreoretinopathy, Coats disease, incontinentia pigmenti, "
     "persistent fetal vasculature)."),
    # "The diagnosis for the second patient is a dissociated vertical
    # deviation. How would you manage her...?" - the reveal question with
    # nothing behind it, on a station where retinoscopy alone held 8 marks.
    (519, "A", "Perform accurate retinoscopy", 4.0, "D",
     "Outlines management of dissociated vertical deviation (e.g. observation "
     "where well controlled, correction of refractive error and amblyopia, "
     "surgery such as superior rectus recession or inferior oblique "
     "anteriorisation if manifest)."),
]

# Deliberately empty, and it stays that way.
#
# Nineteen rubric items are worth 0.0 marks, and a checker that finds "this
# can never be earned" reads them all as the same defect as an unmarked
# question. Reading them says otherwise. "Avoid incorrectly giving bilateral
# CRVO as a primary diagnosis", "Does not suggest PDT or anti-VEGF as primary
# treatment", "Recommending subconjunctival corticosteroid injection, which
# risks scleral melt" - these are the examiners' own zero-weight cautions,
# carried out of the report on purpose. They are worth reading in feedback and
# they cost the candidate nothing, unlike an unmarked question that eats
# ninety seconds of a nine-minute station.
#
# Left here as the record of a flag that was raised and correctly not acted
# on, so the next person does not re-derive it and delete them.
DROP_WORTHLESS: list[int] = []


def _load_env() -> str:
    env_file = BACKEND / ".env.production"
    if not env_file.exists():
        raise SystemExit(f"No env file at {env_file}")
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
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
    from sqlalchemy.orm import Session
    from sqlalchemy.orm.attributes import flag_modified

    from app.models import OsceSession, OsceStation

    engine = sa.create_engine(_load_env(), connect_args={"connect_timeout": 30})
    changed = 0
    with Session(engine) as db:
        sat = {r[0] for r in db.execute(sa.select(OsceSession.station_id)).all()}

        def prompt_of(station, label):
            for prompt in station.prompts or []:
                if prompt.get("label") == label:
                    return prompt
            raise SystemExit(f"station {station.id} has no question {label}")

        touched: set[int] = set()

        for sid, src, fragment, marks, dst, text in REDISTRIBUTE:
            if sid in sat:
                print(f"-- st{sid} already sat, left alone")
                continue
            station = db.get(OsceStation, sid)
            source, target = prompt_of(station, src), prompt_of(station, dst)
            hit = [r for r in (source.get("rubric") or [])
                   if fragment in str(r.get("text") or "")]
            if not hit:
                print(f"!! st{sid} {src}: nothing matching {fragment!r}")
                continue
            item = hit[0]
            if float(item.get("marks") or 0) <= marks:
                print(f"!! st{sid} {src}: {item['marks']}m is not enough to give {marks}m")
                continue
            item["marks"] = round(float(item["marks"]) - marks, 2)
            target.setdefault("rubric", []).append(
                {"text": text, "marks": marks, "is_critical": False}
            )
            print(f"st{sid}: {marks}m from {src} ({item['text'][:48]}...) to {dst}\n"
                  f"    + {text}\n")
            flag_modified(station, "prompts")
            touched.add(sid)
            changed += 1

        for sid in DROP_WORTHLESS:
            if sid in sat:
                print(f"-- st{sid} already sat, left alone")
                continue
            station = db.get(OsceStation, sid)
            for prompt in station.prompts or []:
                dead = [r for r in (prompt.get("rubric") or [])
                        if not float(r.get("marks") or 0)]
                for item in dead:
                    prompt["rubric"].remove(item)
                    print(f"st{sid} {prompt.get('label')}: dropped 0m item "
                          f"{str(item.get('text'))[:70]!r}")
                    flag_modified(station, "prompts")
                    touched.add(sid)
                    changed += 1

        # Nothing above may change what the paper is out of, and no question
        # that was funded may still be unfunded.
        for sid in sorted(touched):
            station = db.get(OsceStation, sid)
            total = sum(r.get("marks", 0) for p in station.prompts
                        for r in (p.get("rubric") or []))
            if round(total, 2) != round(float(station.total_marks), 2):
                raise SystemExit(
                    f"station {sid} would total {total}, not {station.total_marks}")
        for sid, *_, dst, _text in REDISTRIBUTE:
            if sid in sat:
                continue
            station = db.get(OsceStation, sid)
            if not (prompt_of(station, dst).get("rubric") or []):
                raise SystemExit(f"station {sid} question {dst} is still unfunded")

        if args.apply:
            db.commit()
            print(f"\napplied {changed} change(s) across {len(touched)} station(s)")
        else:
            db.rollback()
            print(f"\n{changed} change(s) across {len(touched)} station(s) "
                  f"- re-run with --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
