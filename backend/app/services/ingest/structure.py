"""Turn a raw text block into a structured question or OSCE station.

Segmentation is deterministic; this step is not. The model is given one block
at a time and asked for strict JSON. Everything it returns is validated against
the known RACE mark allocations before it reaches the database, and any
discrepancy is recorded rather than silently accepted.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.constants import (
    SEQ_TOTAL_MARKS,
    SUBSPECIALTIES,
    VSAQ_TOTAL_MARKS,
    normalise_subspecialty,
)
from app.services.ai import AIClient
from app.services.ingest.segment import Block

logger = logging.getLogger(__name__)

MARKS_RE = re.compile(r"\((\d+(?:\.\d+)?)\s*marks?\)", re.IGNORECASE)

WRITTEN_SYSTEM = f"""\
You are transcribing an official RANZCO RACE Written Examiners' Report into \
structured data for an exam simulator. You are a precise transcriber, not an \
author: reproduce what the report says and never invent clinical content.

The nine RANZCO subspecialties are: {", ".join(SUBSPECIALTIES)}.

Rules:
- Copy the question stem and each sub-question verbatim, preserving clinical \
detail, examination findings and tables. Convert tabular findings into readable \
lines (e.g. "VA: RE 6/6, LE 6/9").
- Every sub-question keeps its printed mark value. An SEQ's parts must total \
{SEQ_TOTAL_MARKS} marks; a VSAQ is worth {VSAQ_TOTAL_MARKS}.
- Some questions advance the scenario partway through ("Your interventions \
allow you to proceed..."). Put that text in the following part's "preamble", \
not in the main stem.
- Examiner commentary appears under "General Feedback & Common Mistakes" and \
"Examiners' Impression of the Cohort". Capture each examiner separately, as a \
list of individual bullet points. This commentary is the most valuable content \
in the report - transcribe it fully and do not paraphrase it away.
- If a field is genuinely absent, use null or an empty list. Never guess.

Return ONLY a JSON object of this shape:
{{
  "question_type": "SEQ" | "VSAQ",
  "topic": string | null,
  "subspecialty": one of the nine subspecialties, or null,
  "purpose": string | null,
  "curriculum_standard_raw": string | null,
  "curriculum_codes": [string],
  "stem": string,
  "parts": [
    {{"label": "a", "preamble": string | null, "text": string, "marks": number}}
  ],
  "figures": [{{"label": "Figure 1", "caption": string | null,
                "referenced_in_part": string | null}}],
  "examiner_feedback": [
    {{"examiner_number": 1,
      "common_mistakes": [string],
      "cohort_impression": [string]}}
  ]
}}"""

OSCE_SYSTEM = f"""\
You are transcribing one station from an official RANZCO RACE OSCE Examiners' \
Report into structured data for an exam simulator. The source is a slide deck, \
so text arrives as short fragments under headings such as "Summary of Case", \
"Aim of the Station", "Patient History", "Findings", "Diagnosis" and "Cohort \
Overall Performance"/"Cohort Common Mistakes".

The nine RANZCO subspecialties are: {", ".join(SUBSPECIALTIES)}.

Rules:
- Reassemble the fragments into coherent prose without inventing clinical detail.
- Preserve every measurement exactly (visual acuities, IOPs, refractions, \
motility notation).
- "tasks" are the instructions given to the candidate at the station (e.g. \
"Examine the anterior segment and describe your findings").
- Build a marking "rubric" from the station's aims, findings and diagnosis: \
discrete, markable expectations a competent candidate should demonstrate. \
Rubric marks must total exactly 20.
- Each station lasts 9 minutes.
- "patient_demographic" is the single line the candidate sees before the \
station begins: age band and sex, nothing else. Age bands: "A child", "A young \
boy", "A young girl", "A teenage boy", "A teenage girl", "A young man", "A \
young woman", "A middle-aged man", "A middle-aged woman", "An elderly man", \
"An elderly woman". It must give away nothing else - no symptoms, diagnosis, \
history, surgery, spectacles or head posture. "An elderly woman" is right; \
"An elderly woman with a head tilt" is wrong.

Return ONLY a JSON object of this shape:
{{
  "station_number": number | null,
  "title": string | null,
  "subspecialty": one of the nine subspecialties, or null,
  "patient_demographic": string | null,
  "case_summary": string | null,
  "aims": [string],
  "patient_history": string | null,
  "findings": string | null,
  "diagnosis": string | null,
  "tasks": [{{"prompt": string, "minutes": number | null}}],
  "rubric": [{{"text": string, "marks": number, "is_critical": boolean}}],
  "cohort_performance": string | null,
  "common_mistakes": [string]
}}"""


def structure_written_block(client: AIClient, block: Block, job_id: int | None = None) -> dict[str, Any]:
    figure_hint = _figure_hint(block)
    user = (
        f"Transcribe the following {block.label} from the examiners' report.\n"
        f"{figure_hint}\n\n"
        f"--- BEGIN REPORT EXTRACT ---\n{block.text}\n--- END REPORT EXTRACT ---"
    )
    data = _unwrap(
        client.complete_json(task="structuring", system=WRITTEN_SYSTEM, user=user, job_id=job_id)
    )
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object for {block.label}, got {type(data).__name__}")
    return normalise_written(data, block)


def structure_osce_block(client: AIClient, block: Block, job_id: int | None = None) -> dict[str, Any]:
    """Turn one station's pages into the station record.

    Asked twice when the first reply is not an object. A malformed or truncated
    completion is a one-off, not a statement about the extract: station 6A of
    2020 Semester 1 came back unparseable, the whole station was dropped, and
    the only trace was "Created 17 item(s). Failed: OSCE 6A." in a field nobody
    reads. Recovering it means re-ingesting the document, which pays for the
    other seventeen stations a second time.
    """
    user = (
        f"Transcribe OSCE station {block.number} from the examiners' report.\n\n"
        f"--- BEGIN REPORT EXTRACT ---\n{block.text}\n--- END REPORT EXTRACT ---"
    )
    last: Any = None
    for attempt in (1, 2):
        last = _unwrap(
            client.complete_json(
                task="structuring", system=OSCE_SYSTEM, user=user, job_id=job_id
            )
        )
        if isinstance(last, dict):
            return normalise_osce(last, block)
        logger.warning(
            "Station %s came back as %s, not an object%s",
            block.printed_number or block.number,
            type(last).__name__,
            "; asking once more" if attempt == 1 else "",
        )
    raise ValueError(
        f"Expected a JSON object for station {block.number}, got "
        f"{type(last).__name__} twice"
    )


def _unwrap(data: Any) -> Any:
    """Accept a single-element array wrapper.

    Models occasionally return `[{...}]` instead of `{...}` despite the schema
    in the prompt. Unwrapping costs nothing and avoids losing a whole question
    to a formatting quirk.
    """
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
        return data[0]
    return data


def _figure_hint(block: Block) -> str:
    if not block.images:
        return "This question has no figures."
    labels = [img.label or f"(unlabelled image {i + 1})" for i, img in enumerate(block.images)]
    return (
        f"This question has {len(block.images)} figure(s) extracted from the PDF: "
        f"{', '.join(labels)}. List them in the \"figures\" array in the same order."
    )


# --- Validation and clean-up ---------------------------------------------
def normalise_written(data: dict[str, Any], block: Block) -> dict[str, Any]:
    """Coerce model output into the shape the persistence layer expects.

    Warnings are attached rather than raised: a question whose marks do not sum
    correctly is still worth keeping, but an administrator needs to see it.
    """
    warnings: list[str] = []

    qtype = str(data.get("question_type") or block.kind or "SEQ").upper()
    if qtype not in {"SEQ", "VSAQ"}:
        qtype = block.kind if block.kind in {"SEQ", "VSAQ"} else "SEQ"

    parts_in = data.get("parts") or []
    parts: list[dict[str, Any]] = []
    for index, raw in enumerate(parts_in):
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or "").strip()
        if not text:
            continue
        parts.append(
            {
                "label": (str(raw.get("label")).strip() if raw.get("label") else _letter(index)),
                "preamble": _clean_str(raw.get("preamble")),
                "text": text,
                "marks": _coerce_marks(raw.get("marks")),
                "position": index,
            }
        )

    # A VSAQ is a single 2-mark question; collapse anything the model split up.
    if qtype == "VSAQ" and len(parts) > 1:
        parts = [
            {
                "label": None,
                "preamble": None,
                "text": "\n".join(p["text"] for p in parts),
                "marks": VSAQ_TOTAL_MARKS,
                "position": 0,
            }
        ]

    if not parts:
        warnings.append("No sub-questions were extracted.")

    expected = SEQ_TOTAL_MARKS if qtype == "SEQ" else VSAQ_TOTAL_MARKS
    total = sum(p["marks"] for p in parts)

    # Marks are often printed but occasionally missed by OCR; recover them from
    # "(N marks)" in the part text before reporting a mismatch.
    if total != expected:
        for part in parts:
            if part["marks"] == 0:
                found = MARKS_RE.search(part["text"])
                if found:
                    part["marks"] = float(found.group(1))
        total = sum(p["marks"] for p in parts)

    if parts and total != expected:
        warnings.append(
            f"Sub-question marks total {total:g}, expected {expected} for a {qtype}."
        )

    subspecialty = normalise_subspecialty(data.get("subspecialty")) or normalise_subspecialty(
        data.get("topic")
    )
    if not subspecialty:
        warnings.append("Could not map this question to one of the nine subspecialties.")

    feedback: list[dict[str, Any]] = []
    for raw in data.get("examiner_feedback") or []:
        if not isinstance(raw, dict):
            continue
        feedback.append(
            {
                "examiner_number": _coerce_int(raw.get("examiner_number")),
                "common_mistakes": _string_list(raw.get("common_mistakes")),
                "cohort_impression": _string_list(raw.get("cohort_impression")),
            }
        )

    figures: list[dict[str, Any]] = []
    for index, raw in enumerate(data.get("figures") or []):
        if not isinstance(raw, dict):
            continue
        figures.append(
            {
                "label": _clean_str(raw.get("label")) or f"Figure {index + 1}",
                "caption": _clean_str(raw.get("caption")),
                "referenced_in_part": _clean_str(raw.get("referenced_in_part")),
            }
        )

    return {
        "question_type": qtype,
        "topic": _clean_str(data.get("topic")),
        "subspecialty": subspecialty,
        "purpose": _clean_str(data.get("purpose")),
        "curriculum_standard_raw": _clean_str(data.get("curriculum_standard_raw")),
        "curriculum_codes": _string_list(data.get("curriculum_codes")),
        "stem": str(data.get("stem") or "").strip(),
        "parts": parts,
        "figures": figures,
        "examiner_feedback": feedback,
        "total_marks": int(total) if float(total).is_integer() else total,
        "warnings": warnings,
    }


def normalise_osce(data: dict[str, Any], block: Block) -> dict[str, Any]:
    warnings: list[str] = []

    rubric: list[dict[str, Any]] = []
    for raw in data.get("rubric") or []:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or "").strip()
        if not text:
            continue
        rubric.append(
            {
                "text": text,
                "marks": _coerce_marks(raw.get("marks")) or 1.0,
                "is_critical": bool(raw.get("is_critical")),
            }
        )

    total = sum(item["marks"] for item in rubric)
    if rubric and abs(total - 20) > 0.01:
        warnings.append(f"Rubric marks total {total:g}, expected 20.")

    tasks: list[dict[str, Any]] = []
    for raw in data.get("tasks") or []:
        if isinstance(raw, dict) and raw.get("prompt"):
            tasks.append(
                {"prompt": str(raw["prompt"]).strip(), "minutes": _coerce_int(raw.get("minutes"))}
            )
        elif isinstance(raw, str) and raw.strip():
            tasks.append({"prompt": raw.strip(), "minutes": None})

    subspecialty = normalise_subspecialty(data.get("subspecialty")) or normalise_subspecialty(
        data.get("title")
    )

    return {
        "station_number": _coerce_int(data.get("station_number")) or block.number,
        "title": _clean_str(data.get("title")),
        "subspecialty": subspecialty,
        "patient_demographic": _clean_str(data.get("patient_demographic")),
        "case_summary": _clean_str(data.get("case_summary")),
        "aims": _string_list(data.get("aims")),
        "patient_history": _clean_str(data.get("patient_history")),
        "findings": _clean_str(data.get("findings")),
        "diagnosis": _clean_str(data.get("diagnosis")),
        "tasks": tasks,
        "rubric": rubric,
        "cohort_performance": _clean_str(data.get("cohort_performance")),
        "common_mistakes": _string_list(data.get("common_mistakes")),
        "total_marks": 20,
        "warnings": warnings,
    }


# --- Small coercion helpers ----------------------------------------------
def _letter(index: int) -> str:
    return chr(ord("a") + index) if index < 26 else str(index + 1)


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _string_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    out: list[str] = []
    for item in value:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def _coerce_marks(value: Any) -> float:
    try:
        marks = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, marks)


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
