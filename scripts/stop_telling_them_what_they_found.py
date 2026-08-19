"""Stop questions opening by naming the findings back to the candidate.

prompts.py is explicit: "Never begin 'You have described...', 'You have
found...', 'You noted...'. The candidate says what they found; the examiner
does not say it for them." It also records why - on a live station that wording
told the candidate the wrong thing: they had described nothing yet, and the
station was about a macular schisis.

Thirty-six questions do it, twenty-three on stations never sat. The damage is
worse than style. "You have described vitritis, CMO, and ERM. What is your
leading diagnosis?" hands back the three findings that question A was just
marked on, so a candidate who missed them at A is handed them at B - and one
who found something else is told they were wrong by the examiner rather than
by the eye.

Anchoring to their findings WITHOUT naming them is what the spec asks for, so
"Based on the findings you have described, what is your differential?" is left
alone. So is a trailing reference - "the procedure you have described in the
right eye" supplies nothing - and a hypothetical, "what would you do if you
found perineural invasion".

The repair is to delete the opening sentence. What remains is the question,
which is allowed to name its own subject: "How would you assess whether the
neovascularisation is active or regressed?" asks something without asserting
anything. Where deleting it would strand a pronoun - "What is the significance
of THAT in this patient?" - the replacement is written out in full below
rather than generated.

Only stations never sat. No model, no search, nothing spent.

    python scripts/stop_telling_them_what_they_found.py            # report
    python scripts/stop_telling_them_what_they_found.py --apply
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

VERB = r"(?:described|noted|found|mentioned|identified|seen|observed)"
OPENS_BY_TELLING = re.compile(
    r"^\s*you(?:'ve| have)?\s+" + VERB + r"\b[^.?!]*[.?!]\s*", re.I)
# The harmless forms, left alone.
NAMES_NOTHING = re.compile(
    r"^\s*you(?:'ve| have)?\s+" + VERB +
    r"\s*(?:[,.?]|the\s+(?:findings|features|signs|appearance|history)\b)", re.I)
# What is left must not lean on a pronoun whose antecedent has just been cut.
DANGLES = re.compile(r"^(?:what|how|why)\b[^.?!]*\b(?:that|it|its|them|these|those)\b",
                     re.I)

# Where the remainder would dangle, the whole question, written out.
BY_HAND: dict[tuple[int, str], str] = {
    (551, "B"): "What is the significance of a relative afferent pupillary "
                "defect in this patient?",
    (595, "B"): "What is the relevance of the central corneal thickness in "
                "this patient?",
    (535, "B"): "What other specific risk factor would you be looking for in "
                "this eye before cataract surgery, and how would you assess it?",
    (573, "B"): "What is your differential diagnosis for the ectropion, and "
                "how would you distinguish between the possible causes?",
    # A question may name its own subject - that is asking, not asserting.
    # What it may not do is claim the candidate already said it.
    (678, "B"): "How would you confirm anterior lenticonus on examination, and "
                "what would you expect to see?",
    (674, "B"): "How would you assess the impact of the corneal opacities on "
                "the patient's vision, beyond just visual acuity?",
    (560, "B"): "What is the cause of the corneal scarring in this patient?",
    (488, "B"): "What is your differential for the underlying cause of the "
                "scleral thinning, and what further investigations would you "
                "order to determine the aetiology?",
    (415, "B"): "What would you look for specifically on gonioscopy in this "
                "patient, and what would it tell you?",
    (336, "B"): "What other ocular manifestations of this condition would you "
                "look for, and how would you assess for them?",
}


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
    from sqlalchemy.orm import Session
    from sqlalchemy.orm.attributes import flag_modified

    from app.models import OsceSession, OsceStation

    engine = sa.create_engine(_load_env(), connect_args={"connect_timeout": 30})
    fixed = manual = skipped = 0
    with Session(engine) as db:
        sat = {r[0] for r in db.execute(sa.select(OsceSession.station_id)).all()}
        for station in db.query(OsceStation).order_by(OsceStation.id).all():
            if station.id in sat:
                continue
            for prompt in station.prompts or []:
                text = str(prompt.get("text") or "")
                if not OPENS_BY_TELLING.match(text) or NAMES_NOTHING.match(text):
                    continue
                label = str(prompt.get("label") or "?")
                written = BY_HAND.get((station.id, label))
                if written:
                    new = written
                    manual += 1
                else:
                    new = OPENS_BY_TELLING.sub("", text, count=1).strip()
                    if not new or DANGLES.match(new):
                        # Cutting the assertion would strand a pronoun and
                        # there is no replacement written for it. Better an
                        # imperfect question than an unanswerable one.
                        print(f"!! st{station.id} {label} would dangle, left alone:\n"
                              f"   {text}")
                        skipped += 1
                        continue
                    new = new[0].upper() + new[1:]
                    fixed += 1
                print(f"st{station.id} {label}\n  - {text}\n  + {new}\n")
                prompt["text"] = new
                flag_modified(station, "prompts")

        print(f"{fixed} stripped, {manual} rewritten by hand, {skipped} left alone")
        if args.apply:
            db.commit()
            print("applied")
        else:
            db.rollback()
            print("re-run with --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
