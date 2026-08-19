"""Stop questions handing over what their own rubric marks.

`station_faults` never looked at this: it inspects images, and a question can
hold every image it needs and still print the answer in its own stem. Reading
each rubric item against its stem found 65 candidates across the bank, of
which 19 were real - the rest were knowledge questions naming their own topic,
which is not a leak ("What are the criteria for ROP screening?" against
"States the criteria for ROP screening").

Two shapes, and they need opposite repairs.

Where the station HAS an image, the findings in the stem are redundant with
the picture and simply removing them restores the mark. Station 467 printed
"this patient has fine inferior KPs, a low grade anterior chamber reaction, a
PSC cataract, and vitreous cells and debris" above 6.5 marks, all critical,
for identifying exactly those four - with two slit lamp photographs on screen.

Where the station has NO image, the stem is the substitute for the picture and
taking it away leaves nothing to answer from, which is the failure this bank
has made before and must not make again. Stations 256 and 335 are those: the
marks stay where they are and the ITEM is reworded, from identifying a finding
the candidate cannot see to interpreting the one the examiner has stated.

The paper's total is asserted unchanged after every edit.

No model, no search, nothing spent.

    python scripts/unleak_stems_by_hand.py            # report only
    python scripts/unleak_stems_by_hand.py --apply
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

# (station, label, new question text)
STEMS: list[tuple[int, str, str]] = [
    # --- The reveal question is allowed to name the diagnosis - that is the
    # point of it - but not the complication or contributor the rubric pays
    # separately for recognising.
    (10, "D",
     "The diagnosis is bilateral optic disc drusen. Her left visual acuity is "
     "6/9.5 and she describes visual disturbance in that eye. What has "
     "developed in the left eye, and how would you manage her if she were new "
     "to you and you had just made the diagnosis?"),
    (173, "E",
     "The diagnosis is COVID-vaccine-induced multifocal choroiditis with "
     "secondary CNV, treated with Eylea and steroids, with residual macular "
     "scars. How would you manage her if she were new to you and you had just "
     "made the diagnosis?"),
    (192, "C",
     "The diagnosis is advanced glaucomatous optic neuropathy with progression "
     "at low intraocular pressures. How would you manage him if he were new to "
     "you and you had just made this diagnosis?"),
    (451, "D",
     "The diagnosis is advanced secondary open-angle glaucoma in the context "
     "of Sturge-Weber Syndrome. What sight-threatening complication would you "
     "particularly fear during intraocular surgery in these patients, and how "
     "would you manage it if it occurred?"),
    (465, "C",
     "The diagnosis is multiple cranial nerve palsies secondary to a "
     "petroclival meningioma with brainstem involvement. She also has severe "
     "ptosis, disabling diplopia and aberrant 3rd nerve regeneration, and "
     "wears a contact lens in the right eye. How would you manage her if she "
     "were new to you and you had just made the diagnosis?"),
    (511, "D",
     "The diagnosis is a left fourth nerve palsy. How would you manage him if "
     "he were new to you and you had just made this diagnosis?"),
    (615, "C",
     "The diagnosis is bilateral low flow caroticocavernous fistulae. How "
     "would you manage her if she were new to you and you had just made the "
     "diagnosis?"),
    # Its rubric pays 2 marks for stating the diagnosis, so the question has
    # to ask for it rather than announce it. This is the one reveal question
    # that becomes a diagnosis question, because that is what it marks.
    (620, "E",
     "What is your diagnosis, and how would you manage her if she were new to "
     "your practice today?"),

    # --- "Here are the findings, now describe them", where a picture is on
    # screen and the words were never needed.
    (201, "B",
     "What ancillary test would best demonstrate the corneal contribution to "
     "her vision, and what would you expect it to show?"),
    (264, "C",
     "Here are her OCT and late-phase fluorescein angiogram of the left eye. "
     "What do they show?"),
    (435, "A",
     "You are presented with a 23-year-old physiology student with a 2-month "
     "history of reduced vision in the left eye. He is QuantiFERON Gold "
     "positive. Here are some images from his workup. Describe what they show."),
    (467, "A",
     "Please examine this patient at the slit lamp and describe your findings. "
     "What do they suggest to you?"),
    (533, "A",
     "This patient presents with diplopia following AVM surgery four years "
     "ago. Here are external photographs in the positions of gaze. Please "
     "describe and summarise the findings for me."),
    (534, "A",
     "This patient presents with recent onset double vision. Please describe "
     "the motility findings, and tell me how you would differentiate between a "
     "paretic and a restrictive motility problem in this patient."),
    # No image, but the fix needs none: question B already sends the candidate
    # to do the refraction, so the result can be handed over as its outcome.
    (671, "C",
     "You perform cycloplegic refraction. What would be your leading diagnosis "
     "for her reduced vision?"),
]

# (station, label, fragment of the item to match, replacement item text)
ITEMS: list[tuple[int, str, str, str]] = [
    # Stations with no image at all. The stem must keep the findings, so the
    # item moves from seeing to interpreting - same marks, same question.
    (256, "A", "Identifies the abnormal head posture (AHP) as a right head turn.",
     "Interprets the right head turn, relating its direction to the underlying "
     "motility deficit."),
    (335, "A", "Identifies enophthalmos",
     "Explains the enophthalmos in terms of the expected imaging findings"),
    (335, "A", "Identifies hypoglobus",
     "Explains the hypoglobus in terms of the expected imaging findings"),
    (335, "A", "Identifies dysmotility (limited elevation and abduction)",
     "Explains the limited elevation and abduction in terms of the expected "
     "imaging findings"),
    # Examiner feedback prose imported as a rubric item, so the candidate was
    # credited for having failed. The model answer was already the right way up.
    (217, "C", "Failing to recognise the limitation of available implants",
     "Recognises the limitation of available implants and the likelihood of "
     "residual refractive needs."),
]

# (station, from label, to label, fragment, replacement) - the mark for naming
# the diagnosis, sitting on the question that names it. It belongs on the
# question before the reveal, which is the only place it can be earned.
MOVES: list[tuple[int, str, str, str, str]] = [
    (18, "E", "D", "State the most likely diagnosis",
     "States congenital fibrosis of extraocular muscles as the most likely "
     "diagnosis."),
    (87, "C", "B", "State the correct diagnosis of Adie",
     "States Adie's pupil as the most likely diagnosis."),
]


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
    from sqlalchemy.orm import Session
    from sqlalchemy.orm.attributes import flag_modified

    from app.models import OsceStation

    engine = sa.create_engine(_load_env(), connect_args={"connect_timeout": 30})
    changed = 0
    with Session(engine) as db:

        def prompt_of(station, label):
            for prompt in station.prompts or []:
                if prompt.get("label") == label:
                    return prompt
            raise SystemExit(f"station {station.id} has no question {label}")

        for sid, label, text in STEMS:
            station = db.get(OsceStation, sid)
            prompt = prompt_of(station, label)
            if prompt["text"] == text:
                continue
            print(f"st{sid} {label}\n  - {prompt['text']}\n  + {text}\n")
            prompt["text"] = text
            flag_modified(station, "prompts")
            changed += 1

        for sid, label, old, new in ITEMS:
            station = db.get(OsceStation, sid)
            prompt = prompt_of(station, label)
            hit = [r for r in (prompt.get("rubric") or [])
                   if old in str(r.get("text") or "")]
            if not hit:
                print(f"!! st{sid} {label}: nothing matching {old!r}")
                continue
            print(f"st{sid} {label} item\n  - {hit[0]['text']}\n  + {new}\n")
            hit[0]["text"] = new
            flag_modified(station, "prompts")
            changed += 1

        for sid, src, dst, old, new in MOVES:
            station = db.get(OsceStation, sid)
            source, target = prompt_of(station, src), prompt_of(station, dst)
            hit = [r for r in (source.get("rubric") or [])
                   if old in str(r.get("text") or "")]
            if not hit:
                print(f"!! st{sid} {src}: nothing matching {old!r}")
                continue
            item = hit[0]
            source["rubric"].remove(item)
            item["text"] = new
            target.setdefault("rubric", []).append(item)
            print(f"st{sid} moved {item['marks']}m from {src} to {dst}\n  + {new}\n")
            flag_modified(station, "prompts")
            changed += 1

        # The paper's total has to survive every one of these edits. A move
        # that dropped an item, or a rewrite that lost one, shows up here
        # before it reaches a candidate.
        touched = ({s for s, *_ in STEMS} | {s for s, *_ in ITEMS}
                   | {s for s, *_ in MOVES})
        for sid in sorted(touched):
            station = db.get(OsceStation, sid)
            total = sum(r.get("marks", 0) for p in station.prompts
                        for r in (p.get("rubric") or []))
            if round(total, 2) != round(float(station.total_marks), 2):
                raise SystemExit(
                    f"station {sid} would total {total}, not {station.total_marks}")

        if args.apply:
            db.commit()
            print(f"applied {changed} change(s)")
        else:
            db.rollback()
            print(f"{changed} change(s) - re-run with --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
