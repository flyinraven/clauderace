"""Remove figures that show nothing, and pictures shown twice.

Groups C to F of the never-sat review all reduced to the same two things.

GHOSTS. Forty figures carry no image and no approved words, so the candidate
cannot see them at all. They are not harmless: `station_faults` counts them as
a missing investigation, which is why stations 670, 571 and 588 report "no
image for its investigation" while showing five paper photographs each. They
are also what a repair run would go looking for - and several were never
answerable requests in the first place, having been written from a rubric item
rather than a description of a picture:

    "Localizes the hemianopia to a post-chiasmal lesion (e.g. optic..."
    "reduced visual acuity (6/18); morning glory optic disc anomaly"
    "A set of five unlabelled ancillary test images for a paediatric..."

DUPLICATES. Six figures show a picture already on the same station, matched by
sha256 rather than by caption - station 367 has two figures captioned
"External photograph of the face in primary gaze" that are DIFFERENT images,
and deleting one of those would lose a view. The bound copy is kept over the
unbound one, so no question loses its picture.

Only stations never sat. No model, no search, nothing spent.

    python scripts/clear_figures_nobody_can_see.py            # report
    python scripts/clear_figures_nobody_can_see.py --apply
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

    from app.api.osce.helpers import _bound_figure_ids
    from app.models import Image, OsceSession, OsceStation

    engine = sa.create_engine(_load_env(), connect_args={"connect_timeout": 30})
    ghosts = dupes = 0
    with Session(engine) as db:
        sat = {r[0] for r in db.execute(sa.select(OsceSession.station_id)).all()}
        sha = dict(db.execute(sa.select(Image.id, Image.sha256)).all())

        for station in db.query(OsceStation).options(
                selectinload(OsceStation.figures)).order_by(OsceStation.id).all():
            if station.id in sat:
                continue
            bound = {i for p in (station.prompts or []) for i in _bound_figure_ids(p)}
            drop: list = []

            for figure in station.figures:
                shows_image = bool(figure.image_id and figure.is_approved)
                says_words = bool(figure.described_findings
                                  and figure.described_findings_approved)
                if not shows_image and not says_words:
                    want = (figure.wanted_description or "")[:58]
                    print(f"st{station.id} ghost fig{figure.id} "
                          f"[{figure.verification_status}] {want!r}")
                    drop.append(figure)
                    ghosts += 1

            seen: dict[str, int] = {}
            for figure in sorted(station.figures, key=lambda f: f.position):
                if figure in drop or not (figure.image_id and figure.is_approved):
                    continue
                digest = sha.get(figure.image_id)
                if digest is None:
                    continue
                if digest not in seen:
                    seen[digest] = figure.id
                    continue
                first = seen[digest]
                # Keep whichever a question points at; a question that loses
                # its picture is a worse station than one showing it twice.
                loser = figure.id if (first in bound or figure.id not in bound) else first
                if loser == first:
                    seen[digest] = figure.id
                victim = next(f for f in station.figures if f.id == loser)
                print(f"st{station.id} duplicate fig{victim.id} repeats "
                      f"fig{first if loser != first else figure.id} "
                      f"({(victim.caption or '')[:44]!r})")
                drop.append(victim)
                dupes += 1

            if not drop:
                continue
            gone = {f.id for f in drop}
            for prompt in station.prompts or []:
                ids = [i for i in _bound_figure_ids(prompt) if i not in gone]
                if ids != _bound_figure_ids(prompt):
                    prompt["figure_ids"] = ids
                    prompt.pop("figure_id", None)
                    flag_modified(station, "prompts")
            for figure in drop:
                db.delete(figure)

        print(f"\n{ghosts} invisible figure(s), {dupes} duplicate(s)")
        if args.apply:
            db.commit()
            print("applied")
        else:
            db.rollback()
            print("re-run with --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
