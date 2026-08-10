"""Take the station's findings out from under its photographs.

A figure's `described_findings` is meant to be the last resort of the image
protocol: no picture could be found, so the examiner states that view's signs
in words. Two paths wrote it under pictures instead, and both wrote the WHOLE
station rather than the view:

  * the verbatim floor, which quotes the station's recorded findings when the
    model declines to describe a view it was given no description of; and
  * `describe_findings` itself, asked with no view in hand, which then
    describes the case.

Either way the same paragraph lands under every figure of the station. Station
1A of 2020 Semester 2 carries "Histology revealed melanoma in situ" beneath
all five of its figures - the external photograph, the ultrasound and a blank
image included - so the answer is on screen before the candidate has looked at
anything. Those words pass the leak guard honestly, because the paper records
the histology as a finding and every word of it is therefore grounded.

The signature is repetition: one block of text shared by two or more figures
that have images. A real description is of one picture and is unique to it.
The floor's exact quote of the recorded findings is taken too, even where a
station has only one figure to smear it on.

Words are not moved anywhere. They already exist as the station's findings,
which is where they belong; under a photograph they are a caption that is not
a caption. A picture with no words says nothing untrue, and is visible as a
gap in the admin page.

    python scripts/clear_smeared_descriptions.py --from-env --dry-run
    python scripts/clear_smeared_descriptions.py --from-env
"""

from __future__ import annotations

import argparse
import collections
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.models import OsceStation  # noqa: E402
from app.services.osce.sittability import opening_figures  # noqa: E402

from audit_station_images import url_from_env  # noqa: E402


# How much of the text has to agree before two descriptions count as one. Long
# enough that two genuine descriptions of different pictures will differ inside
# it - "optical coherence tomography of the right macula shows..." diverges well
# before sixty characters - and short enough to catch a rephrased tail.
PREFIX = 60


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--database-url")
    group.add_argument("--from-env", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    engine = create_engine(url_from_env() if args.from_env else args.database_url)

    with Session(engine) as db:
        cleared = stations = on_opening = 0
        for station in db.execute(select(OsceStation)).scalars():
            # Only figures that HAVE a picture. A figure without one is the
            # protocol working as designed and its words are all the candidate
            # gets.
            shown = [
                f for f in station.figures
                if f.image_id is not None and _norm(f.described_findings)
            ]
            if not shown:
                continue

            # Matched on the opening of the text, not the whole of it. The
            # model rephrases as it goes, so the same account of the same case
            # arrives six times with six different tails: station 12 of 2023
            # Semester 1 describes an anterior segment under all six of its
            # FUNDUS photographs, each ending differently. An exact-match rule
            # cleared the identical ones and left those, which are the worse
            # case - words that do not merely fail to describe the picture but
            # describe a different part of the eye entirely.
            counts = collections.Counter(
                _norm(f.described_findings)[:PREFIX] for f in shown
            )
            recorded = {
                _norm(station.findings_elicited)[:PREFIX],
                _norm(station.findings)[:PREFIX],
            } - {""}
            smeared = {t for t, n in counts.items() if n > 1} | recorded

            hit = [f for f in shown if _norm(f.described_findings)[:PREFIX] in smeared]
            if not hit:
                continue

            opening = {f.id for f in opening_figures(station)}
            stations += 1
            cleared += len(hit)
            on_opening += sum(1 for f in hit if f.id in opening)
            if not args.dry_run:
                for figure in hit:
                    figure.described_findings = None
                    figure.described_findings_approved = False

        print(f"{cleared} figure(s) across {stations} station(s); "
              f"{on_opening} were on an opening screen.")
        if args.dry_run:
            print("\n--dry-run: nothing written.")
            return 0
        db.commit()

    print("\nCleared. The pictures stand on their own; the findings are still "
          "recorded on the station, which is where a candidate is meant to "
          "reach them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
