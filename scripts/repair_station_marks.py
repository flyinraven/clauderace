"""Make every OSCE station's rubric marks whole again.

Rubrics are scaled to fit the station's 20 marks. That scaling used to round to
two decimals, which produced rubric lines worth 1.54 and 3.06 marks and
sub-questions headed "9.99 marks" - allocations no examiner could award and no
candidate could be marked against. Apportionment now keeps the proportions and
lands on half marks, the granularity clamp_award has always accepted (see
`marking.rescale_marks_to_awardable`).

Stations built after that fix are already whole; this repairs the ones built
before it, in place, without spending anything on the model. It is the cheap
alternative to "Rebuild all questions", which re-runs the prompt builder over
every station and bills for each one.

Both mark stores are repaired: the flat `rubric` column and the per-question
slices inside `prompts`, which is what the station review screen totals.

Past results are unaffected - an OsceGrade records the marks it was marked out
of at the time - but a station that has already been sat will show a rubric
that no longer matches that attempt's breakdown. `--skip-sat` leaves those
alone; the summary always says how many are involved.

    python scripts/repair_station_marks.py --database-url "postgresql+psycopg://..." --dry-run
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
from sqlalchemy.orm.attributes import flag_modified  # noqa: E402

from app.models import OsceSession, OsceStation  # noqa: E402
from app.services.marking import rescale_marks_to_awardable  # noqa: E402

STATION_MARKS = 20


def _is_awardable(points: list[dict]) -> bool:
    """Already a whole or half mark, so an examiner could award it."""
    return all((float(p.get("marks") or 0) * 2).is_integer() for p in points)


def _marks(points: list[dict]) -> float:
    return round(sum(float(p.get("marks") or 0) for p in points), 2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--skip-sat", action="store_true",
        help="leave stations this platform has already recorded a sitting for",
    )
    args = parser.parse_args()

    engine = create_engine(args.database_url)
    repaired = 0
    skipped_sat = 0
    sat_and_repaired = 0
    declined = 0

    with Session(engine) as db:
        sat_station_ids = {
            station_id
            for (station_id,) in db.execute(
                select(OsceSession.station_id).group_by(OsceSession.station_id)
            ).all()
        }

        stations = db.execute(select(OsceStation).order_by(OsceStation.id)).scalars().all()
        for station in stations:
            prompts = station.prompts or []
            prompt_points = [pt for p in prompts for pt in (p.get("rubric") or [])]
            flat_points = list(station.rubric or [])
            if not prompt_points and not flat_points:
                continue
            if _is_awardable(prompt_points) and _is_awardable(flat_points):
                continue

            was_sat = station.id in sat_station_ids
            if was_sat and args.skip_sat:
                skipped_sat += 1
                continue

            name = station.title or f"Station {station.station_number or station.id}"
            # The station total was already ~20; what was wrong is that no
            # individual allocation was a whole mark. Show those.
            before = " ".join(
                f"{p.get('label') or '?'}={_marks(p.get('rubric') or []):g}" for p in prompts
            ) or f"{len(flat_points)} lines"

            # Each store must total 20 on its own, so each is apportioned
            # separately rather than one being derived from the other.
            ok = True
            for points in (prompt_points, flat_points):
                if points and not rescale_marks_to_awardable(points, STATION_MARKS):
                    ok = False
            if not ok:
                declined += 1
                print(
                    f"  ! {station.id:>4} {name[:42]:42s} "
                    f"{len(prompt_points) or len(flat_points)} rubric lines share "
                    f"{STATION_MARKS} marks - left fractional"
                )
                continue

            after = " ".join(
                f"{p.get('label') or '?'}={_marks(p.get('rubric') or []):g}" for p in prompts
            ) or f"{len(flat_points)} lines"
            print(
                f"  {station.id:>4} {name[:38]:38s} {before}  ->  {after}"
                + ("  (sat)" if was_sat else "")
            )
            if was_sat:
                sat_and_repaired += 1

            if not args.dry_run:
                # The rubric dicts were mutated in place, and they are the very
                # objects SQLAlchemy loaded, so its before/after comparison sees
                # no change and emits no UPDATE - reassigning the column is not
                # enough either. flag_modified is what actually marks it.
                flag_modified(station, "prompts")
                flag_modified(station, "rubric")
            repaired += 1

        if not args.dry_run:
            db.commit()

    verb = "would repair" if args.dry_run else "repaired"
    print(f"\n{verb} {repaired} station(s)")
    if sat_and_repaired:
        print(
            f"  {sat_and_repaired} of them have been sat; past results keep the marks "
            f"they were graded against, but their rubric now reads differently"
        )
    if skipped_sat:
        print(f"  skipped {skipped_sat} sat station(s) at your request")
    if declined:
        print(f"  {declined} station(s) have more rubric lines than marks and were left alone")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
