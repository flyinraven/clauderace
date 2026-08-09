"""Give marks to the questions that carry none.

The prompt builder is told the marks must total 20 and that is checked. Nothing
said every question must be worth some, so the model concentrated them: 147
questions across 98 stations ended up worth nothing - one station with three of
its six - and the marker replies "This question carries no marks" to an answer
that took a minute of a nine-minute station to give. 154 minutes of clock time
across the bank buys nothing.

The rule is now enforced where prompts are built, so this is for the stations
built before it.

**Only the marks move.** The question text is left exactly as it is. Those
stems were rewritten twice on 7-8 August - trimmed to the images that actually
arrived, and made to ask for a differential before the diagnosis is revealed -
and rebuilding the questions would discard all of it and change what images
each one wants, which would mean sourcing the bank again. A rebuild is the
expensive way to fix an arithmetic problem.

**The model writes wording; this file does the arithmetic.** The first attempt
asked the model to allocate the marks too, and 84 of 98 stations came back not
totalling 20. That was the wrong job to give it - hitting an exact sum across
six questions is arithmetic, and a language model is the least reliable way to
do arithmetic.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.models import OsceStation
from app.services.ai import AIClient
from app.services.coerce import as_float
from app.services.errors import log_error
from app.services.jobs.runner import JobContext, JobHandlerError, register_handler
from app.services.marking import absorb_mark_drift, rescale_marks_to_awardable

logger = logging.getLogger(__name__)

JOB_REMARK_STATIONS = "remark_osce_stations"

STATION_MARKS = 20.0

SYSTEM_PROMPT = """\
You are a RANZCO examiner writing the marking key for questions at an OSCE \
station that were left without one.

You are given the whole station, so you can see what has already been asked and \
credited, and then the questions carrying no marks. Write the points an \
examiner would tick for each.

A point is something the candidate SAYS, marked present or absent - "Identifies \
the inferior corneal thinning", "Names posterior polymorphous dystrophy as a \
differential" - not a judgement of them, and never "Understands keratoconus". \
Draw them from what the question asks and from the station's findings and \
diagnosis. Do not repeat a point another question already credits.

Write between 2 and 4 points for each question. Do NOT assign marks: the marks \
are worked out separately and any you wrote would be discarded.

Return ONLY a JSON object mapping each named label to its points:
{"C": ["...", "..."], "E": ["...", "...", "..."]}"""


def _dead_lines(prompt: dict[str, Any]) -> list[int]:
    """Rubric lines worth nothing, on a question that is worth something.

    The same fault one level down, and it hides from the question-level check:
    "Name four causes" carrying four lines and 1.5 marks pays for three of
    them, and the fourth can never be awarded however well it is answered.
    """
    return [
        index
        for index, point in enumerate(prompt.get("rubric") or [])
        if as_float(point.get("marks"), 0.0) <= 0
    ]


def stations_needing_marks(db: Session) -> list[int]:
    """Stations with a question - or a single line - worth nothing."""
    out = []
    for station_id, prompts in db.execute(
        select(OsceStation.id, OsceStation.prompts)
    ).all():
        for prompt in prompts or []:
            rubric = prompt.get("rubric") or []
            if sum(as_float(pt.get("marks"), 0.0) for pt in rubric) <= 0 or _dead_lines(prompt):
                out.append(station_id)
                break
    return out


def rebalance_marks(prompts: list[dict[str, Any]]) -> dict[str, float] | None:
    """What each question should be worth when none of them is dead.

    `plan_marks` is for reviving a question worth nothing, and pays for it out
    of the others. This is the quieter case: every question carries marks, but
    one of them carries fewer than its own rubric lines cost, so a line inside
    it is worth nothing and cannot be awarded.

    Each question is lifted to what its lines cost, and the lift is taken from
    the questions furthest above their own floor - largest first, so the marks
    come off where they are least felt. Returns None when the station's twenty
    marks cannot cover what its rubric asks for, which is a refusal: the
    question needs fewer lines, and that is not arithmetic.
    """
    if not prompts:
        return None
    floors = {str(p.get("label")): _floor_for(p) for p in prompts}
    if sum(floors.values()) > STATION_MARKS + 0.001:
        return None

    targets = {str(p.get("label")): _question_marks(p) for p in prompts}
    for label, floor in floors.items():
        targets[label] = max(targets[label], floor)

    # Give back whatever the lifting took, from those with most to spare.
    drift = round(sum(targets.values()) - STATION_MARKS, 2)
    while drift > 0.001:
        spare = [l for l in targets if targets[l] - floors[l] >= 0.5]
        if not spare:
            return None
        label = max(spare, key=lambda l: targets[l] - floors[l])
        targets[label] -= 0.5
        drift = round(drift - 0.5, 2)
    while drift < -0.001:
        label = max(targets, key=lambda l: targets[l])
        targets[label] += 0.5
        drift = round(drift + 0.5, 2)

    if abs(sum(targets.values()) - STATION_MARKS) > 0.01:
        return None
    return targets


def drop_lines_that_are_not_points(
    station: OsceStation, prompts: list[dict[str, Any]]
) -> int:
    """Remove rubric lines that are the examiners' notes, not marking points.

    Station 19 carries "Not mentioning lack of subretinal fluid" as a rubric
    line worth nothing - word for word what its `common_mistakes` already says.
    Ingest put the same sentence in both places, and a candidate cannot say a
    thing they failed to mention: it is a note about the cohort, not a point
    anybody can earn.

    Matched against the station's own mistakes rather than by how the sentence
    reads. A rule about wording would eventually meet a real point that starts
    with "Not", and this needs no guessing: the sentence is already recorded
    elsewhere as what it actually is.
    """
    mistakes = {
        str(m).strip().rstrip(".").lower()
        for m in (station.common_mistakes or [])
        if str(m).strip()
    }
    if not mistakes:
        return 0
    dropped = 0
    for prompt in prompts:
        kept = [
            point
            for point in (prompt.get("rubric") or [])
            if not (
                as_float(point.get("marks"), 0.0) <= 0
                and str(point.get("text") or "").strip().rstrip(".").lower() in mistakes
            )
        ]
        dropped += len(prompt.get("rubric") or []) - len(kept)
        prompt["rubric"] = kept
    return dropped


def rebalance_station(db: Session, station: OsceStation) -> dict[str, Any]:
    """Make every line on a station awardable. No model call: this is arithmetic."""
    prompts = [
        dict(p, rubric=[dict(pt) for pt in (p.get("rubric") or [])])
        for p in (station.prompts or [])
    ]
    if not prompts:
        return {"skipped": 1}

    dropped = drop_lines_that_are_not_points(station, prompts)
    if not any(_dead_lines(p) for p in prompts) and not dropped:
        return {"already_marked": 1}

    targets = rebalance_marks(prompts)
    if targets is None:
        return {"rejected": 1, "reason": "twenty marks cannot cover the lines the rubric asks for"}

    for prompt in prompts:
        prompt["rubric"] = _spread(prompt["rubric"], targets[str(prompt.get("label"))])

    total = _total(prompts)
    still = [
        f"{p.get('label')}[{i}]" for p in prompts for i in _dead_lines(p)
    ]
    if still or abs(total - STATION_MARKS) > 0.01:
        return {"rejected": 1, "reason": f"totals {total:g}, unawardable lines: {still or 'none'}"}

    station.prompts = prompts
    flag_modified(station, "prompts")
    station.total_marks = int(STATION_MARKS)
    db.commit()
    return {"rebalanced": 1, "lines_dropped": dropped}


def _half(value: float) -> float:
    """Marks are whole numbers or halves; nothing finer is defensible."""
    return round(value * 2) / 2


def _question_marks(prompt: dict[str, Any]) -> float:
    return sum(as_float(pt.get("marks"), 0.0) for pt in (prompt.get("rubric") or []))


def _total(prompts: list[dict[str, Any]]) -> float:
    return sum(_question_marks(p) for p in prompts)


def plan_marks(prompts: list[dict[str, Any]]) -> dict[str, float] | None:
    """What each question should be worth. Pure arithmetic, no model.

    A dead question is paid for out of the questions holding more than their
    share, in proportion to what they hold, and is worth a share of the 20
    proportional to the time it is given on the clock - which is the station's
    own statement of how much it matters. Every question keeps at least one
    mark. Returns None when there is no allocation that works, which is a
    refusal, not a repair.
    """
    dead = [p for p in prompts if _question_marks(p) <= 0]
    alive = [p for p in prompts if _question_marks(p) > 0]
    if not dead or not alive:
        return None

    total_seconds = sum(as_float(p.get("seconds"), 0.0) for p in prompts)
    targets: dict[str, float] = {}
    for prompt in dead:
        seconds = as_float(prompt.get("seconds"), 0.0)
        share = (
            STATION_MARKS * seconds / total_seconds
            if total_seconds
            else STATION_MARKS / len(prompts)
        )
        # Capped: a question that was worth nothing should not come back as the
        # most valuable one on the station.
        targets[str(prompt.get("label"))] = min(max(_half(share), 1.0), 4.0)

    pool = STATION_MARKS - sum(targets.values())
    if pool < len(alive):
        return None  # the survivors could not keep a mark each

    held = sum(_question_marks(p) for p in alive)
    for prompt in alive:
        scaled = pool * _question_marks(prompt) / held if held else pool / len(alive)
        # Never below what its own rubric lines cost. A question the examiners
        # wrote four points for cannot be cut to one mark without one of those
        # points being worth nothing, and they are not this pass's to discard.
        targets[str(prompt.get("label"))] = max(_floor_for(prompt), _half(scaled))

    # Rounding leaves a remainder either way. It goes on the largest question,
    # where half a mark is least visible - and comes off the largest question
    # that can afford it, which is not always the same one.
    floors = {str(p.get("label")): _floor_for(p) for p in alive}
    drift = STATION_MARKS - sum(targets.values())
    if abs(drift) > 0.001:
        payable = [
            label for label in targets
            if targets[label] + drift >= floors.get(label, 1.0)
        ]
        if not payable:
            return None
        largest = max(payable, key=lambda k: targets[k])
        targets[largest] = targets[largest] + drift
    if abs(sum(targets.values()) - STATION_MARKS) > 0.01:
        return None
    return targets


def _floor_for(prompt: dict[str, Any]) -> float:
    """The least a question can be worth and still pay for the points it holds.

    Half a mark is the finest award anyone can make, so a question carrying
    four rubric lines cannot be worth one mark: something has to be awarded
    nothing. This is what keeps the allocation from proposing a share the
    question cannot spend.
    """
    return max(1.0, 0.5 * len(prompt.get("rubric") or []))


def _spread(points: list[dict[str, Any]], marks: float) -> list[dict[str, Any]]:
    """Share one question's marks across its points, evenly, in awardable halves.

    Every point had a floor of half a mark and the remainder was pushed onto
    one of them, which cannot go below zero. A question worth 1 mark holding
    four points therefore came out worth 1.5, and the half mark surfaced on the
    station total: eleven stations were refused for totalling 20.5 with every
    question marked, which reads as a fault in the allocation and was this.

    `rescale_marks_to_awardable` is the apportionment that cannot drift - the
    same one the written papers use - and it refuses outright when there are
    more lines than half marks to pay them with. That refusal is the case
    above, so the lines that cannot be awarded go rather than being awarded
    nothing. Only a question whose points were written by the re-marking model
    can reach that state; an examiner's own lines are paid for by the floor in
    `plan_marks`.
    """
    if not points:
        return points
    units = int(round(marks * 2))
    if units <= 0:
        return points
    del points[units:]

    # Seeded equal, so the apportionment shares them evenly - which is what
    # this has always done. The rescale reads the marks already on a point as
    # the ratio to preserve, and equal seeds mean equal shares.
    for point in points:
        point["marks"] = 1.0
    if not rescale_marks_to_awardable(points, marks):
        each = _half(marks / len(points)) or 0.5
        for point in points:
            point["marks"] = each
        absorb_mark_drift(points, marks)
    return points


def remark_station(
    db: Session, client: AIClient, station: OsceStation, job_id: int | None = None
) -> dict[str, Any]:
    """Give every question marks, keeping the station at 20."""
    prompts = [dict(p) for p in (station.prompts or [])]
    if not prompts:
        return {"skipped": 1}
    unmarked = [str(p.get("label")) for p in prompts if _question_marks(p) <= 0]
    if not unmarked:
        return {"already_marked": 1}

    targets = plan_marks(prompts)
    if targets is None:
        return {"rejected": 1, "reason": "no workable allocation of the 20 marks"}

    listing = "\n\n".join(
        f"[{p.get('label')}] ({_question_marks(p):g} marks now) {p.get('text')}\n"
        + "\n".join(f"    - {pt.get('text')}" for pt in (p.get("rubric") or []))
        for p in prompts
    )
    user = (
        f"SUBSPECIALTY: {station.subspecialty or 'unspecified'}\n"
        f"DIAGNOSIS: {station.diagnosis or 'not recorded'}\n"
        f"FINDINGS:\n{station.findings_elicited or station.findings or '(none)'}\n\n"
        f"MISTAKES THE EXAMINERS NOTED:\n{station.common_mistakes or '(none)'}\n\n"
        f"THE STATION AS IT STANDS:\n{listing}\n\n"
        f"Write the marking points for these questions, which have none: "
        f"{', '.join(unmarked)}"
    )
    data = client.complete_json(
        task="utility", system=SYSTEM_PROMPT, user=user, job_id=job_id
    )
    if not isinstance(data, dict):
        raise ValueError("Re-marking did not return a JSON object")

    for prompt in prompts:
        label = str(prompt.get("label"))
        if label in unmarked:
            texts = [
                str(t).strip()
                for t in (data.get(label) or [])
                if isinstance(t, str) and str(t).strip()
            ]
            if not texts:
                return {"rejected": 1, "reason": f"no marking points written for {label}"}
            prompt["rubric"] = _spread(
                [{"text": t, "is_critical": False} for t in texts], targets[label]
            )
        else:
            # Scaled, not rewritten. A point that survives with fewer marks is
            # still a point the candidate is credited for; a deleted one is a
            # thing they said and were not.
            prompt["rubric"] = _spread(
                [dict(pt) for pt in (prompt.get("rubric") or [])], targets[label]
            )

    total = _total(prompts)
    still_dead = [str(p.get("label")) for p in prompts if _question_marks(p) <= 0]
    if still_dead or abs(total - STATION_MARKS) > 0.01:
        return {
            "rejected": 1,
            "reason": f"totals {total:g}, still worth nothing: {still_dead or 'none'}",
        }

    station.prompts = prompts
    flag_modified(station, "prompts")
    station.total_marks = int(STATION_MARKS)
    db.commit()
    return {"remarked": 1, "questions_given_marks": len(unmarked)}


@register_handler(JOB_REMARK_STATIONS)
def handle_remark_stations(ctx: JobContext) -> bool:
    """One station per chunk."""
    station_ids: list[int] = ctx.payload.get("station_ids") or []
    if not station_ids:
        raise JobHandlerError("No station_ids supplied")
    if not ctx.job.total_steps:
        ctx.set_total(len(station_ids))

    index = ctx.cursor_get("index", 0)
    if index >= len(station_ids):
        return True
    if ctx.cancelled:
        return True

    station = ctx.db.get(OsceStation, station_ids[index])
    if station is not None:
        try:
            # A question worth nothing needs wording, and wording needs the
            # model. A line worth nothing inside a question that has marks
            # needs neither - the points are already written and only the
            # arithmetic is wrong, so it is not paid for.
            if any(_question_marks(p) <= 0 for p in (station.prompts or [])):
                outcome = remark_station(ctx.db, AIClient(ctx.db), station, job_id=ctx.job.id)
            else:
                outcome = rebalance_station(ctx.db, station)
        except Exception as exc:  # noqa: BLE001 - one station must not stop the run
            ctx.db.rollback()
            logger.exception("Could not re-mark station %s", station.id)
            log_error(ctx.db, source="osce_remark", message=str(exc),
                      context={"station_id": station.id})
            outcome = {"failed": 1}

        # A refusal used to be counted and forgotten, so 84 of 98 stations
        # declined for reasons nobody could read. It is recorded now.
        if outcome.get("rejected"):
            log_error(
                ctx.db,
                source="osce_remark",
                level="warning",
                message=f"Station {station.id} left as it was: {outcome.get('reason')}",
                context={"station_id": station.id},
            )

        running = dict(ctx.job.result or {})
        for key, value in outcome.items():
            if isinstance(value, int):
                running[key] = running.get(key, 0) + value
        ctx.set_result(**running)

    ctx.cursor_set(index=index + 1)
    ctx.advance(1, f"Re-marked {index + 1} of {len(station_ids)} stations")
    return index + 1 >= len(station_ids)
