"""Take the answer out from under the picture the candidate walks in on.

A description written beneath an opening figure is read before the candidate
has said anything. Where it names the diagnosis, every diagnostic mark on the
station is free: station 3B of 2020 Semester 2 opened with "A dislocated PMMA
IOL is present" against a diagnosis of "Dislocated IOL".

`leaked_term` lets those through on purpose. It guards the last resort of the
image protocol, where the words ARE the station and striking them leaves the
candidate nothing - a stricter version binned 37 good descriptions of 38. But
that reasoning does not survive contact with a photograph. Striking the words
under a picture costs a caption and keeps the picture, so the strict rule is
affordable exactly here, and `names_the_diagnosis` applies it: a word of the
diagnosis is refused however well the findings ground it.

Only figures that open the station. A figure a question owns appears when that
question does, and by then the examiner has asked - naming what a pathology
slide shows is the point of showing it.

    python scripts/hush_opening_descriptions.py --from-env --dry-run
    python scripts/hush_opening_descriptions.py --from-env --skip-sat
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.models import OsceCircuit, OsceStation  # noqa: E402
from app.services.osce.sittability import opening_figures  # noqa: E402
from app.services.osce.station_images.verify import names_the_diagnosis  # noqa: E402

from audit_station_images import url_from_env  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--database-url")
    group.add_argument("--from-env", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--skip-sat", action="store_true",
        help="leave stations that have already appeared in a circuit alone - "
             "they will not be met again, so changing them buys nothing",
    )
    args = parser.parse_args()

    engine = create_engine(url_from_env() if args.from_env else args.database_url)

    with Session(engine) as db:
        sat: set[int] = set()
        if args.skip_sat:
            for circuit in db.execute(select(OsceCircuit)).scalars():
                sat.update(circuit.station_ids or [])

        cleared = stations = skipped = 0
        for station in db.execute(select(OsceStation)).scalars():
            if station.id in sat:
                skipped += 1
                continue
            hit = []
            for figure in opening_figures(station):
                words = (figure.described_findings or "").strip()
                if figure.image_id is None or not words:
                    continue
                stated = names_the_diagnosis(words, station)
                if stated:
                    hit.append((figure, stated))
            if not hit:
                continue
            stations += 1
            cleared += len(hit)
            print(f"station {station.id} (#{station.station_label or station.station_number}"
                  f", {station.exam_period}): "
                  + ", ".join(f"fig{f.position} says {s!r}" for f, s in hit))
            if not args.dry_run:
                for figure, _ in hit:
                    figure.described_findings = None
                    figure.described_findings_approved = False

        print(f"\n{cleared} opening description(s) across {stations} station(s)."
              + (f" {skipped} already-sat station(s) left alone." if sat else ""))
        if args.dry_run:
            print("\n--dry-run: nothing written.")
            return 0
        db.commit()

    print("\nHushed. The photographs are untouched, and the findings are still "
          "recorded on the station.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
