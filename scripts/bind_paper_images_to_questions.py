"""Put the papers' own images under the questions that ask for them.

The examiners' reports carry the real photographs, and 1159 of them are
approved on stations never sat. 225 are attached to a question. The other 934
belong to no question at all, which means they are either shown all at once
before anything is asked - an MRI on screen from the beginning answers the
question before it is put - or, if `opening_figures_payload` holds them back as
investigations, shown nowhere and wasted.

Meanwhile the questions that ask for them go unillustrated and get a stock
picture searched from the station's subspecialty, because the fallback query
when a figure has no description is the word "Vitreoretinal".

Two free repairs, no model and no search:

BIND. Where a question names a modality, holds nothing of that modality, and
the station has an unbound paper image whose caption is that modality, the
image goes under the question. Real photograph, right question, no spend.

DESCRIBE. Where an approved figure has no `wanted_description` at all - 982 of
them - write one from what the image is and what the station is about. That is
the text any future search will use, and today its absence is what makes the
search fall back to a one-word subspecialty name. Nothing is re-sourced here;
this only stops the next search being hopeless.

Only stations never sat.

    python scripts/bind_paper_images_to_questions.py            # report only
    python scripts/bind_paper_images_to_questions.py --apply
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
    parser.add_argument("--limit", type=int, default=0, help="stop after N stations")
    args = parser.parse_args()

    import sqlalchemy as sa
    from sqlalchemy.orm import Session, selectinload
    from sqlalchemy.orm.attributes import flag_modified

    from app.api.osce.helpers import _bound_figure_ids
    from app.models import OsceSession, OsceStation
    from app.services.osce.sittability import _MODALITY_WORDS

    def modalities(text: str) -> set[str]:
        return {n for n, rx in _MODALITY_WORDS.items() if rx.search(text or "")}

    engine = sa.create_engine(_load_env(), connect_args={"connect_timeout": 30})
    bound = described = 0
    with Session(engine) as db:
        sat = {r[0] for r in db.execute(sa.select(OsceSession.station_id)).all()}
        stations = db.query(OsceStation).options(
            selectinload(OsceStation.figures)).order_by(OsceStation.id).all()

        for station in stations:
            if station.id in sat or not (station.prompts or []):
                continue
            if args.limit and bound >= args.limit:
                break
            by_id = {f.id: f for f in station.figures}
            owned = {i for p in station.prompts for i in _bound_figure_ids(p)}

            # --- BIND
            for prompt in station.prompts:
                asked = modalities(str(prompt.get("text") or ""))
                if not asked:
                    continue
                # Already illustrated with the right kind of picture: leave it.
                held = set()
                for fid in _bound_figure_ids(prompt):
                    figure = by_id.get(fid)
                    if figure and figure.image_id and figure.is_approved:
                        held |= modalities(figure.caption)
                if asked & held:
                    continue
                # The caption must be the asked-for modality and nothing else.
                # Matching on overlap alone put "Bilateral optic disc OCT
                # report" under a question wanting a fundus photograph (the
                # phrase "optic disc" is in that pattern) and "Fluorescein
                # angiogram of the right fundus" under another (the word
                # "fundus"). An investigation is not a photograph of the eye.
                candidates = [
                    f for f in station.figures
                    if f.id not in owned and f.image_id and f.is_approved
                    and f.verification_status == "from_paper"
                    and (modalities(f.caption) & asked)
                    and not (modalities(f.caption) - asked)
                ]
                if not candidates:
                    continue
                # Laterality decides between otherwise equal candidates. A
                # question about the left eye must not be handed the right.
                text = str(prompt.get("text") or "").lower()
                side = ("left" if "left" in text else
                        "right" if "right" in text else None)
                if side:
                    same = [f for f in candidates
                            if side in (f.caption or "").lower()]
                    if same:
                        candidates = same
                    elif any(("left" if side == "right" else "right")
                             in (f.caption or "").lower() for f in candidates):
                        # Every candidate names the other eye. Binding one
                        # would put the wrong eye under the question, which is
                        # the fault being repaired.
                        continue
                pick = sorted(candidates, key=lambda f: f.position)[0]
                ids = list(_bound_figure_ids(prompt)) + [pick.id]
                prompt["figure_ids"] = ids
                prompt.pop("figure_id", None)
                owned.add(pick.id)
                flag_modified(station, "prompts")
                print(f"st{station.id} {prompt.get('label')} asks {sorted(asked)}"
                      f" -> fig{pick.id} {pick.caption!r}")
                bound += 1

            # --- DESCRIBE
            for figure in station.figures:
                if not (figure.image_id and figure.is_approved):
                    continue
                if (figure.wanted_description or "").strip():
                    continue
                caption = (figure.caption or "").strip().rstrip(".")
                if not caption:
                    continue
                subject = (station.diagnosis or station.title or "").strip().rstrip(".")
                figure.wanted_description = (
                    f"{caption}, showing {subject}" if subject else caption
                )
                described += 1

        print(f"\nbound {bound} paper image(s) to the question that asks for them; "
              f"wrote {described} missing search description(s)")
        if args.apply:
            db.commit()
            print("applied")
        else:
            db.rollback()
            print("re-run with --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
