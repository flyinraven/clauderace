"""Put back the station photographs a bad pass detached.

On 2 August a pass over the review queue detached every figure whose image had
been rejected for showing a different investigation than the station's opening
task, intending to state the findings in words instead. Those images were not
web lookalikes: they were the photographs printed in the examiners' own reports,
the ones the real candidates were shown. Verification no longer drops them, but
the links it already cleared do not come back on their own.

The images themselves were never deleted - only `osce_figures.image_id` was set
to null - so this restores the link from a backup taken before the pass ran.

Deliberately narrow. A figure is restored only when all of these hold:

  * the backup has an image for it and the live row does not;
  * that image still exists in the `images` table;
  * nobody has rejected it by hand since - a rejection also clears the image,
    and putting back a picture an administrator turned down would be a second
    wrong. Both the rejection count and the note are checked.

Where the same pass also wrote findings into a figure that had none before, the
words are cleared as the image returns: they were written because there was no
picture, and a station showing both would be describing what is in front of the
candidate.

    python scripts/restore_detached_figures.py --database-url "..." \\
        --backup backups/race_2026-08-02_0057.sql.gz --dry-run
"""

from __future__ import annotations

import argparse
import gzip
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.models import Image, OsceFigure  # noqa: E402

TABLE = "osce_figures"
# Written by the reject endpoint. A figure carrying it lost its image because a
# human said so, and it is not this script's business.
ADMIN_REJECTION = "Rejected by the administrator"


def _unescape(value: str) -> str | None:
    r"""pg_dump's COPY format: \N is null, and tabs/newlines are escaped."""
    if value == r"\N":
        return None
    return (
        value.replace(r"\r", "\r").replace(r"\n", "\n")
        .replace(r"\t", "\t").replace(r"\\", "\\")
    )


def read_backup_figures(path: Path) -> dict[int, dict[str, str | None]]:
    """Pull the osce_figures rows out of a plain-format pg_dump.

    Streamed rather than loaded: the dump is well over a hundred megabytes and
    all that is wanted is one table's worth of columns.
    """
    rows: dict[int, dict[str, str | None]] = {}
    columns: list[str] = []
    inside = False

    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not inside:
                if line.startswith("COPY ") and f".{TABLE} (" in line:
                    header = line[line.index("(") + 1 : line.index(")")]
                    columns = [c.strip().strip('"') for c in header.split(",")]
                    inside = True
                continue
            if line.startswith("\\."):
                break
            values = line.rstrip("\n").split("\t")
            if len(values) != len(columns):
                continue
            row = {col: _unescape(val) for col, val in zip(columns, values)}
            if row.get("id") is not None:
                rows[int(row["id"])] = row

    if not inside:
        raise SystemExit(f"No COPY block for {TABLE} found in {path}")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    backup = read_backup_figures(args.backup)
    print(f"read {len(backup)} figure row(s) from {args.backup.name}")

    engine = create_engine(args.database_url)
    restored = 0
    words_cleared = 0
    skipped_rejected = 0
    skipped_no_image = 0
    unchanged = 0

    with Session(engine) as db:
        live = db.execute(
            select(OsceFigure).where(OsceFigure.image_id.is_(None))
        ).scalars().all()
        have_image = {
            i for (i,) in db.execute(select(Image.id)).all()
        }

        for figure in live:
            was = backup.get(figure.id)
            if was is None or was.get("image_id") is None:
                unchanged += 1
                continue

            if ADMIN_REJECTION in (figure.verification_notes or ""):
                skipped_rejected += 1
                continue
            before_count = int(was.get("rejection_count") or 0)
            if (figure.rejection_count or 0) > before_count:
                skipped_rejected += 1
                continue

            image_id = int(was["image_id"])
            if image_id not in have_image:
                skipped_no_image += 1
                continue

            figure.image_id = image_id
            figure.verification_status = was.get("verification_status") or "unverified"
            figure.is_approved = (was.get("is_approved") or "").lower() in {"t", "true"}
            # Words written only because the picture had gone.
            if (figure.described_findings or "").strip() and not (
                was.get("described_findings") or ""
            ).strip():
                figure.described_findings = None
                figure.described_findings_approved = False
                words_cleared += 1
            restored += 1

        if not args.dry_run:
            db.commit()

    verb = "would restore" if args.dry_run else "restored"
    print(f"\n{verb} {restored} figure(s) to their paper's own photograph")
    if words_cleared:
        print(f"  {words_cleared} of them had stand-in wording, cleared with the image back")
    if skipped_rejected:
        print(f"  left {skipped_rejected} alone: an administrator rejected those by hand")
    if skipped_no_image:
        print(f"  left {skipped_no_image} alone: the image row is no longer in the database")
    print(f"  {unchanged} image-less figure(s) had no image in the backup either")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
