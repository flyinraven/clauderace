"""Give back the tier the comparison pass wrote over.

The describe/compare pass was allowed to stamp `verification_status =
"described"` on figures that HAVE an image. "described" is meant for the last
resort - no picture, findings stated in words - so writing it over a figure
that has a picture erased the one thing that says how good that picture is:
`from_paper`, `faithful` or `representative`. The audit reads that field, so
every `representative_only` fault on 337 figures went quiet at once.

The code no longer does this (describe.py only sets "described" when
`image_id is None`). This repairs the rows it already spoiled.

Nothing here asks a model anything. The pass overwrote one column and left the
evidence beside it intact, so the tier is reconstructed rather than re-judged:

  * origin == "pdf"  -> `from_paper`. Not a judgement: the examiners' report
    printed this image. Confidence there scores how well it matches the
    station, and a poor score is a reason to look again, never a reason to
    pretend the paper did not print it.

  * origin == "web"  -> `faithful` when `match_confidence >= 0.7` and the notes
    carry neither downgrade marker; `representative` otherwise. That is the
    rule sourcing.py applied when it set the tier, read back off what it wrote:
    a blind-disagreement downgrade clamps confidence to 0.55, and only a
    representative ever gets a "Does NOT show" note.

The rule was checked against the 108 web figures the pass never touched, whose
tiers are still the originals. It reproduces 107 of them. The miss is one
figure recorded `representative` at high confidence with no note explaining
why, which this restores as `faithful`. Anything the rule is unsure of it
calls `representative`, because over-claiming `faithful` is the direction that
hides a fault from the audit.

    python scripts/restore_overwritten_tiers.py --from-env --dry-run
    python scripts/restore_overwritten_tiers.py --from-env
"""

from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.models import Image, OsceFigure  # noqa: E402
from app.services.osce.station_images.constants import (  # noqa: E402
    FROM_PAPER,
    MIN_MATCH_CONFIDENCE,
)

from audit_station_images import url_from_env  # noqa: E402

# Written into the notes when the tier was lowered. Either one means the image
# is a picture of the right disease and the wrong patient.
DOWNGRADE_MARKERS = ("Does NOT show", "Looked at without the station")


def tier_for(figure: OsceFigure, origin: str) -> str:
    if origin == "pdf":
        return FROM_PAPER
    notes = figure.verification_notes or ""
    if any(marker in notes for marker in DOWNGRADE_MARKERS):
        return "representative"
    if (figure.match_confidence or 0.0) >= MIN_MATCH_CONFIDENCE:
        return "faithful"
    return "representative"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--database-url")
    group.add_argument("--from-env", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    engine = create_engine(url_from_env() if args.from_env else args.database_url)

    with Session(engine) as db:
        rows = db.execute(
            select(OsceFigure, Image.origin)
            .join(Image, Image.id == OsceFigure.image_id)
            .where(OsceFigure.verification_status == "described")
        ).all()

        tally: collections.Counter[tuple[str, str]] = collections.Counter()
        stations: set[int] = set()
        for figure, origin in rows:
            tier = tier_for(figure, origin)
            tally[(origin, tier)] += 1
            stations.add(figure.station_id)
            if not args.dry_run:
                figure.verification_status = tier

        if not rows:
            print("No figure holds an image and the word 'described'. Nothing to restore.")
            return 0

        for (origin, tier), count in sorted(tally.items()):
            print(f"{origin:<5} -> {tier:<16} {count}")
        print(f"\n{len(rows)} figure(s) across {len(stations)} station(s).")

        if args.dry_run:
            print("\n--dry-run: nothing written.")
            return 0
        db.commit()

    print("\nRestored. Re-run scripts/audit_station_images.py: the "
          "representative_only faults it reports will be honest again, and "
          "there will be more of them than before.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
