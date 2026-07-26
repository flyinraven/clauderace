"""Split an extracted document into per-question / per-station blocks.

Segmentation is deliberately deterministic rather than model-driven: the
RANZCO reports have a rigid structure, splitting on it costs nothing, and it
means one badly-parsed question cannot corrupt its neighbours. The model is
then asked to structure one block at a time, which also keeps each request
small enough to be reliable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.services.ingest.extract import ExtractedDocument, ExtractedImage

# "SEQ 1", "SEQ1", "Question 3" on a line of its own.
SEQ_HEADER_RE = re.compile(r"^\s*(?:SEQ|Short\s+Essay\s+Question)\s*[#:]?\s*(\d{1,2})\s*$", re.IGNORECASE)
VSAQ_HEADER_RE = re.compile(r"^\s*(?:VSAQ|Very\s+Short\s+Answer\s+Question)\s*[#:]?\s*(\d{1,2})\s*$", re.IGNORECASE)
QUESTION_HEADER_RE = re.compile(r"^\s*Question\s*[#:]?\s*(\d{1,2})\s*$", re.IGNORECASE)
STATION_RE = re.compile(r"\bstation\s*0*(\d{1,2})\b", re.IGNORECASE)

# Page furniture to drop before handing text to the model.
NOISE_RE = re.compile(
    r"^\s*(?:page\s+\d+\s+of\s+\d+|RACE\s+(?:OSCE|Written).*|"
    r"This report has been prepared by the Royal Australian.*|"
    r"\(electronic or otherwise\).*|Permission may be refused.*|"
    r"copyright\. Except as permitted.*)\s*$",
    re.IGNORECASE,
)


@dataclass
class Block:
    """One question or station's worth of raw material."""

    kind: str  # "SEQ" | "VSAQ" | "OSCE"
    number: int | None
    text: str
    page_numbers: list[int] = field(default_factory=list)
    images: list[ExtractedImage] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.kind} {self.number}" if self.number else self.kind


def _clean_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if not NOISE_RE.match(line)]


def detect_document_kind(doc: ExtractedDocument) -> str:
    """Classify an upload as an OSCE report, a written report, or unknown."""
    text = doc.full_text
    station_hits = len(STATION_RE.findall(text))
    seq_hits = len(
        [l for l in text.splitlines() if SEQ_HEADER_RE.match(l) or QUESTION_HEADER_RE.match(l)]
    )
    if station_hits >= 10 and station_hits > seq_hits:
        return "osce"
    if seq_hits >= 2:
        return "written"
    return "unknown"


def segment_written(doc: ExtractedDocument) -> list[Block]:
    """Split a written examiners' report into one block per SEQ/VSAQ."""
    # (page_number, line) across the whole document, minus furniture.
    lines: list[tuple[int, str]] = []
    for page in doc.pages:
        for line in _clean_lines(page.text):
            lines.append((page.number, line))

    starts: list[tuple[int, str, int]] = []  # (line index, kind, number)
    for idx, (_page, line) in enumerate(lines):
        for regex, kind in ((SEQ_HEADER_RE, "SEQ"), (VSAQ_HEADER_RE, "VSAQ")):
            match = regex.match(line)
            if match:
                starts.append((idx, kind, int(match.group(1))))
                break
        else:
            match = QUESTION_HEADER_RE.match(line)
            # Only treat a bare "Question N" as a boundary when the document
            # has no explicit SEQ headers, so we do not split on the "Question:"
            # sub-heading inside every SEQ.
            if match and not any(SEQ_HEADER_RE.match(l) for _p, l in lines):
                starts.append((idx, "SEQ", int(match.group(1))))

    if not starts:
        return []

    blocks: list[Block] = []
    for position, (start_idx, kind, number) in enumerate(starts):
        end_idx = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        chunk = lines[start_idx:end_idx]
        if not chunk:
            continue
        page_numbers = sorted({page for page, _ in chunk})
        blocks.append(
            Block(
                kind=kind,
                number=number,
                text="\n".join(line for _page, line in chunk).strip(),
                page_numbers=page_numbers,
            )
        )

    _assign_images_by_owner(doc, blocks)
    return blocks


def _assign_images_by_owner(doc: ExtractedDocument, blocks: list[Block]) -> None:
    """Give each figure to exactly one block.

    Consecutive questions share a page boundary, so page-overlap alone would
    attach a boundary-page figure to both neighbours. A question's figures
    always sit after its header, so the owner is the last block that *starts*
    on or before the figure's page.
    """
    if not blocks:
        return
    # (start page, block) in document order.
    starts = [(block.page_numbers[0], block) for block in blocks if block.page_numbers]

    for page in doc.pages:
        for image in page.images:
            owner = None
            for start_page, block in starts:
                if start_page <= page.number:
                    owner = block
                else:
                    break
            if owner is not None:
                owner.images.append(image)


def segment_osce(doc: ExtractedDocument) -> list[Block]:
    """Group the slides of an OSCE report into one block per station.

    The deck repeats "Station NN" on every slide, so pages are grouped by the
    station number they mention. A slide naming four or more stations is the
    contents page and is skipped.
    """
    grouped: dict[int, list[int]] = {}
    order: list[int] = []

    for page in doc.pages:
        numbers = {int(n) for n in STATION_RE.findall(page.text)}
        if not numbers or len(numbers) >= 4:
            continue
        station = min(numbers)
        if station not in grouped:
            grouped[station] = []
            order.append(station)
        grouped[station].append(page.number)

    blocks: list[Block] = []
    for station in sorted(order):
        page_numbers = grouped[station]
        text_parts: list[str] = []
        for page in doc.pages:
            if page.number in page_numbers:
                text_parts.extend(_clean_lines(page.text))
        blocks.append(
            Block(
                kind="OSCE",
                number=station,
                text="\n".join(text_parts).strip(),
                page_numbers=page_numbers,
                images=_images_for_pages(doc, page_numbers),
            )
        )
    return blocks


def segment(doc: ExtractedDocument, kind: str | None = None) -> tuple[str, list[Block]]:
    kind = kind or detect_document_kind(doc)
    if kind == "osce":
        return kind, segment_osce(doc)
    return kind, segment_written(doc)


def _images_for_pages(doc: ExtractedDocument, page_numbers: list[int]) -> list[ExtractedImage]:
    wanted = set(page_numbers)
    images: list[ExtractedImage] = []
    for page in doc.pages:
        if page.number in wanted:
            images.extend(page.images)
    return images
