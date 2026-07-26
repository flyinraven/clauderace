"""Assemble exam papers from the question bank.

A paper must match its RANZCO shape (Paper 1: 5 SEQ + 15 VSAQ, and so on) and
should spread subspecialties the way a real paper does rather than drawing four
glaucoma questions in a row.
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.constants import (
    DEFAULT_ANGOFF_EXPECTED,
    QUESTION_SEQ,
    QUESTION_VSAQ,
    STATUS_APPROVED,
    PaperSpec,
)
from app.models import ExamPaper, ExamPaperQuestion, Question


class AssemblyError(RuntimeError):
    """Raised when the bank cannot satisfy a paper's requirements."""


@dataclass
class AssemblyReport:
    seq_selected: int
    vsaq_selected: int
    seq_required: int
    vsaq_required: int
    subspecialties: dict[str, int]
    shortfalls: list[str]


def available_counts(db: Session) -> dict[str, int]:
    rows = db.execute(
        select(Question.question_type, Question.id).where(Question.status == STATUS_APPROVED)
    ).all()
    counts: dict[str, int] = defaultdict(int)
    for question_type, _ in rows:
        counts[question_type] += 1
    return dict(counts)


def _pick_spread(questions: list[Question], wanted: int, rng: random.Random) -> list[Question]:
    """Choose `wanted` questions, spreading subspecialties as evenly as possible.

    Round-robins across subspecialty buckets so a paper samples broadly before
    it takes a second question from any one area.
    """
    buckets: dict[str, list[Question]] = defaultdict(list)
    for question in questions:
        buckets[question.subspecialty or "Unclassified"].append(question)
    for bucket in buckets.values():
        rng.shuffle(bucket)

    order = sorted(buckets, key=lambda k: -len(buckets[k]))
    chosen: list[Question] = []
    while len(chosen) < wanted:
        took_any = False
        for key in order:
            if not buckets[key]:
                continue
            chosen.append(buckets[key].pop())
            took_any = True
            if len(chosen) == wanted:
                break
        if not took_any:
            break
    return chosen


def assemble_paper(
    db: Session,
    spec: PaperSpec,
    title: str,
    created_by_id: int | None = None,
    seed: int | None = None,
    strict: bool = True,
) -> tuple[ExamPaper, AssemblyReport]:
    rng = random.Random(seed)

    pool = db.execute(
        select(Question).where(Question.status == STATUS_APPROVED)
    ).scalars().all()
    seqs = [q for q in pool if q.question_type == QUESTION_SEQ]
    vsaqs = [q for q in pool if q.question_type == QUESTION_VSAQ]

    shortfalls: list[str] = []
    if len(seqs) < spec.seq_count:
        shortfalls.append(
            f"{spec.seq_count} approved SEQs required, {len(seqs)} available"
        )
    if len(vsaqs) < spec.vsaq_count:
        shortfalls.append(
            f"{spec.vsaq_count} approved VSAQs required, {len(vsaqs)} available"
        )
    if shortfalls and strict:
        raise AssemblyError("; ".join(shortfalls))

    chosen_seqs = _pick_spread(seqs, spec.seq_count, rng)
    chosen_vsaqs = _pick_spread(vsaqs, spec.vsaq_count, rng)

    paper = ExamPaper(
        title=title,
        paper_type="written",
        day=spec.day,
        paper_number=spec.number,
        description=(
            f"Part A: {len(chosen_seqs)} SEQ · Part B: {len(chosen_vsaqs)} VSAQ · "
            f"{spec.writing_minutes} min writing time"
        ),
        created_by_id=created_by_id,
    )
    db.add(paper)
    db.flush()

    for position, question in enumerate(chosen_seqs):
        db.add(
            ExamPaperQuestion(
                paper_id=paper.id, question_id=question.id, section="A", position=position
            )
        )
    for position, question in enumerate(chosen_vsaqs):
        db.add(
            ExamPaperQuestion(
                paper_id=paper.id, question_id=question.id, section="B", position=position
            )
        )

    selected = chosen_seqs + chosen_vsaqs
    paper.total_marks = sum(q.total_marks for q in selected)
    paper.cut_score = compute_cut_score(selected)
    db.commit()
    db.refresh(paper)

    spread: dict[str, int] = defaultdict(int)
    for question in selected:
        spread[question.subspecialty or "Unclassified"] += 1

    return paper, AssemblyReport(
        seq_selected=len(chosen_seqs),
        vsaq_selected=len(chosen_vsaqs),
        seq_required=spec.seq_count,
        vsaq_required=spec.vsaq_count,
        subspecialties=dict(spread),
        shortfalls=shortfalls,
    )


def compute_cut_score(questions: list[Question]) -> float:
    """Angoff cut score: the marks a borderline candidate would be expected to earn.

    Each question carries `angoff_expected`, the fraction of its marks a
    just-at-standard candidate would score. Summing marks x expectation across
    the paper gives the pass mark, which is why the cut score differs between
    papers of differing difficulty - exactly the point of Angoff standard
    setting.
    """
    total = 0.0
    for question in questions:
        expected = question.angoff_expected
        if expected is None:
            expected = DEFAULT_ANGOFF_EXPECTED
        total += float(question.total_marks) * float(expected)
    return round(total, 2)


def recompute_cut_score(db: Session, paper: ExamPaper) -> float:
    questions = [
        db.get(Question, item.question_id) for item in paper.items
    ]
    questions = [q for q in questions if q is not None]
    paper.total_marks = sum(q.total_marks for q in questions)
    paper.cut_score = compute_cut_score(questions)
    db.commit()
    return paper.cut_score
