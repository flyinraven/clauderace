"""Name the condition a station records everywhere except its diagnosis field.

Eleven stations have no diagnosis. Not because the report withheld one - block
2A of 2020 Semester 2 has no DIAGNOSIS heading at all - but because it is
stated in the SUMMARY OF CASE instead: "An infant with pseudoesotropia", "a
patient with orbital inflammatory disease", "a 24-year-old female with
congenital toxoplasmosis".

Two things break when that field is empty, and neither is obvious:

  * `leaked_term` matches a description against the diagnosis, so an empty
    diagnosis means the guard passes everything. The background block and the
    stated findings on those eleven stations could name the condition freely,
    and nothing would notice.
  * `describe_findings` needs the findings or the diagnosis to describe from.
    Station 270 has neither, so its representative image - which the vision
    model says does not show the eyes in different positions of gaze - has no
    words beside it and the marks cannot be earned.

Extraction, not diagnosis: the model is given the case summary and asked which
condition it names, and told to return nothing if it names none. A station
whose summary is genuinely silent keeps its empty field.

    python scripts/recover_missing_diagnoses.py --from-env --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.models import Job  # noqa: E402
from app.models.ops import JOB_PENDING  # noqa: E402
from app.services.osce.diagnosis import (  # noqa: E402
    JOB_RECOVER_DIAGNOSES,
    stations_missing_a_diagnosis,
)

from audit_station_images import url_from_env  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--database-url")
    group.add_argument("--from-env", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    engine = create_engine(url_from_env() if args.from_env else args.database_url)
    with Session(engine) as db:
        ids = stations_missing_a_diagnosis(db)
        print(f"{len(ids)} station(s) with no diagnosis but a case summary")
        for station_id in ids:
            print(f"  {station_id}")
        if args.dry_run:
            print("\n--dry-run: nothing queued.")
            return 0
        # Queued rather than run here: the API keys are encrypted with the
        # production key, so anything needing a model has to run on the server.
        job = Job(
            job_type=JOB_RECOVER_DIAGNOSES,
            status=JOB_PENDING,
            payload={"station_ids": ids},
            cursor={},
            total_steps=len(ids),
            message=f"Reading the diagnosis of {len(ids)} station(s)",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        print(f"\nQueued job {job.id}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
