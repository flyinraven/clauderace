"""Play the questions in the order they were designed, and show the picture.

Two defects met in the circuit of 19 Aug 2026, both free to repair.

ORDER. Every question carries a `step` - the arc the prompt builder writes to:
examine, investigate, interpret, differentiate, reveal, evolve, knowledge. The
labels were assigned in a different order and the station plays back in label
order, so station 691 asked about growth hormone replacement, a step 2
question, last - after the candidate had already managed the patient and
discussed the long-term complications. 82 stations do this.

PICTURES. A question bound to a words-only stand-in while the image it
describes sits on the same station. Station 217 question C displayed the
sentence "You are shown a printout displaying intraocular lens power
calculations for both eyes" - and figure 1064 IS that printout, approved and
present. The candidate scored 0 of 3.

Rebinding is not enough on its own. A stand-in left behind is no longer owned
by any question, and an unowned figure is shown from the start, so its words
would move from beside its question to the top of the station - a findings
leak in place of a fixed binding. So the stand-in goes once its question
points at the real image, and only where the match is unambiguous.

Only stations never sat, and only where every question has a step. A station
already sat has grades keyed to its labels.

    python scripts/reorder_and_rebind.py            # report only
    python scripts/reorder_and_rebind.py --apply
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

LABELS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

_SKIP = frozenset("""the a an and or of in on for with to this is are was were be as at by from
into over under not no any all both each patient eye eyes left right bilateral you your here
shown showing show displaying displayed""".split())


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", (text or "").lower()) if w not in _SKIP}


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
    from sqlalchemy.orm import Session, selectinload
    from sqlalchemy.orm.attributes import flag_modified

    from app.api.osce.helpers import _bound_figure_ids
    from app.models import OsceSession, OsceStation

    engine = sa.create_engine(_load_env(), connect_args={"connect_timeout": 30})
    reordered = rebound = dropped = skipped = 0
    with Session(engine) as db:
        sat = {r[0] for r in db.execute(sa.select(OsceSession.station_id)).all()}
        stations = db.query(OsceStation).options(
            selectinload(OsceStation.figures)).order_by(OsceStation.id).all()

        for station in stations:
            if station.id in sat:
                continue
            prompts = station.prompts or []
            if not prompts:
                continue

            # --- the picture the question should have been showing
            pictures = [f for f in station.figures if f.image_id and f.is_approved]
            for prompt in prompts:
                for fid in list(_bound_figure_ids(prompt)):
                    stand_in = next((f for f in station.figures if f.id == fid), None)
                    if stand_in is None or stand_in.image_id or not pictures:
                        continue
                    if not stand_in.described_findings:
                        continue
                    wanted = _words(stand_in.wanted_description
                                    or stand_in.described_findings)
                    if not wanted:
                        continue
                    scored = []
                    for pic in pictures:
                        caption = _words(pic.caption)
                        if not caption:
                            continue
                        scored.append((len(wanted & caption) / len(caption), pic))
                    scored.sort(key=lambda x: x[0], reverse=True)
                    # Unambiguous only. A near-tie means two images could be
                    # meant and guessing puts the wrong one under the question,
                    # which is the fault this is repairing.
                    if not scored or scored[0][0] < 0.6:
                        skipped += 1
                        continue
                    if len(scored) > 1 and scored[1][0] >= scored[0][0] - 0.15:
                        skipped += 1
                        continue
                    picture = scored[0][1]
                    ids = [i for i in _bound_figure_ids(prompt) if i != fid]
                    if picture.id not in ids:
                        ids.append(picture.id)
                    prompt["figure_ids"] = ids
                    prompt.pop("figure_id", None)
                    print(f"st{station.id} {prompt.get('label')}: fig{fid} (words) "
                          f"-> fig{picture.id} {picture.caption!r}")
                    db.delete(stand_in)
                    flag_modified(station, "prompts")
                    rebound += 1
                    dropped += 1

            # --- the order the arc was written in
            steps = [int(p.get("step") or 0) for p in prompts]
            if 0 in steps or steps == sorted(steps):
                continue
            order = sorted(range(len(prompts)), key=lambda i: (steps[i], i))
            if len(prompts) > len(LABELS):
                continue
            before = "".join(str(p.get("label") or "?")[0] for p in prompts)
            new = [prompts[i] for i in order]
            for index, prompt in enumerate(new):
                prompt["label"] = LABELS[index]
            station.prompts = new
            flag_modified(station, "prompts")
            after = " ".join(f"{p['label']}({p.get('step')})" for p in new)
            print(f"st{station.id}: {before} -> {after}")
            reordered += 1

        # Nothing may be lost by either edit.
        for station in stations:
            if station.id in sat:
                continue
            labels = [p.get("label") for p in station.prompts or []]
            if len(labels) != len(set(labels)):
                raise SystemExit(f"station {station.id} has duplicate labels {labels}")
            total = sum(r.get("marks", 0) for p in (station.prompts or [])
                        for r in (p.get("rubric") or []))
            if station.prompts and round(total, 2) != round(float(station.total_marks), 2):
                raise SystemExit(
                    f"station {station.id} totals {total}, not {station.total_marks}")

        print(f"\nreordered {reordered} station(s); rebound {rebound} question(s) "
              f"to a real image, dropping {dropped} stand-in(s); "
              f"{skipped} binding(s) left alone as ambiguous")
        if args.apply:
            db.commit()
            print("applied")
        else:
            db.rollback()
            print("re-run with --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
