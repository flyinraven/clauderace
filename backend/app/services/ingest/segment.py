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
# "Station 7", and "Station 1A" / "Station 1B" - a paper that splits its
# stations into two halves, each a station in its own right. The suffix must be
# captured, not merely tolerated: `\b` after the digits does not match between
# "1" and "A", so the older pattern found no stations at all in such a deck and
# the whole document scored zero station hits. 2022 Semester 2 is numbered this
# way and would not ingest.
#
# The letter is deliberately not allowed to follow a space: "Station 1 A patient
# is seated..." must read as station 1, not station 1A.
STATION_RE = re.compile(r"\bstation\s*0*(\d{1,2})([A-Za-z])?\b", re.IGNORECASE)

# Every station opens by stating its case and what it is testing, whatever the
# year's slide furniture looks like. Some decks number their stations in the
# footer and some label them only by subspecialty and day, so this — not the
# station number — is what reliably marks where one station ends and the next
# begins.
CASE_START_RE = re.compile(r"aim\s+of\s+the\s+station|summary\s+of\s+case", re.IGNORECASE)

# A station's opening runs over two slides often enough that its summary and its
# aim can land on different pages. Starts this close together are one station.
CASE_START_MIN_GAP = 2

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
    # "A" or "B" where a paper splits station 1 into 1A and 1B. Two stations,
    # sat separately, sharing a printed number.
    suffix: str | None = None

    @property
    def printed_number(self) -> str:
        """How the paper itself names this station, e.g. "1A"."""
        return f"{self.number}{self.suffix or ''}" if self.number else ""

    @property
    def label(self) -> str:
        return f"{self.kind} {self.printed_number}" if self.number else self.kind


def _clean_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if not NOISE_RE.match(line)]


def detect_document_kind(doc: ExtractedDocument) -> str:
    """Classify an upload as an OSCE report, a written report, or unknown."""
    text = doc.full_text
    station_hits = len(STATION_RE.findall(text))
    seq_hits = len(
        [l for l in text.splitlines() if SEQ_HEADER_RE.match(l) or QUESTION_HEADER_RE.match(l)]
    )
    # Decks that label stations by subspecialty and day mention "Station" barely
    # a handful of times, so the case marker is what identifies them.
    case_hits = len(CASE_START_RE.findall(text))
    if case_hits >= 4 and case_hits > seq_hits:
        return "osce"
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

    Preferred route is to cut the deck where each station introduces its case,
    because that marker survives every deck format we have. Only when a deck
    lacks it do we fall back to grouping by the station number in the footer,
    which is absent from the years labelled by subspecialty and day.
    """
    by_case = _segment_osce_by_case(doc)
    if len(by_case) >= 2:
        return by_case
    return _segment_osce_by_station_number(doc)


def _segment_osce_by_case(doc: ExtractedDocument) -> list[Block]:
    """One block per station, cut at each station's opening slide."""
    starts: list[int] = []
    for page in doc.pages:
        if not CASE_START_RE.search(page.text):
            continue
        if starts and page.number - starts[-1] <= CASE_START_MIN_GAP:
            continue
        starts.append(page.number)

    blocks: list[Block] = []
    found: list[int | None] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] - 1 if position + 1 < len(starts) else doc.page_count
        pages = [page for page in doc.pages if start <= page.number <= end]
        if not pages:
            continue
        text = "\n".join("\n".join(_clean_lines(page.text)) for page in pages).strip()
        numbered = STATION_RE.search(text)
        page_numbers = [page.number for page in pages]
        blocks.append(
            Block(
                kind="OSCE",
                number=int(numbered.group(1)) if numbered else position + 1,
                suffix=(numbered.group(2) or "").upper() or None if numbered else None,
                text=text,
                page_numbers=page_numbers,
                images=_images_for_pages(doc, page_numbers),
            )
        )
        found.append(
            (int(numbered.group(1)), (numbered.group(2) or "").upper())
            if numbered else None
        )

    # Prefer the deck's own numbering, but only when the whole deck carries it
    # and it runs in order. A single "Station 16" bleeding across a slide
    # boundary would otherwise give two stations the same number, and position
    # in the deck is the order sat regardless.
    #
    # The suffix is part of the identity here: 1A and 1B are in order and are
    # not duplicates of each other, so a paper numbered that way keeps its own
    # names instead of being renumbered 1..18.
    if not (all(n is not None for n in found) and found == sorted(set(found))):
        for position, block in enumerate(blocks):
            block.number = position + 1
            block.suffix = None
    return blocks


def _segment_osce_by_station_number(doc: ExtractedDocument) -> list[Block]:
    """Group by the "Station NN" repeated on every slide.

    A slide naming four or more stations is the contents page and is skipped.
    """
    # Keyed by number AND suffix: 1A and 1B are two stations, and grouping them
    # under "1" would merge two papers' worth of pages into one station and lose
    # half the deck.
    grouped: dict[tuple[int, str], list[int]] = {}
    order: list[tuple[int, str]] = []

    # A page belongs to the last station named, not only to a page that names
    # one. The photographs are on slides of their own - no heading, no text -
    # and keeping only titled pages dropped them before ingest ever saw them:
    # 2022 Semester 2 lost 43 of its 54 clinical images that way, station 6A
    # keeping pages 66, 67, 71, 72 while the four photographs on 68 to 70 went
    # in the bin and the station was then sent to buy replacements off the web.
    current: tuple[int, str] | None = None
    for page in doc.pages:
        stations = {(int(n), (s or "").upper()) for n, s in STATION_RE.findall(page.text)}
        # A slide naming four or more stations is the contents page. It belongs
        # to no station and must not end the one in progress.
        if len(stations) >= 4:
            continue
        if stations:
            current = min(stations)
        if current is None:
            # Front matter, before the first station is named.
            continue
        if current not in grouped:
            grouped[current] = []
            order.append(current)
        grouped[current].append(page.number)

    blocks: list[Block] = []
    for station in sorted(order):
        number, suffix = station
        page_numbers = grouped[station]
        text_parts: list[str] = []
        for page in doc.pages:
            if page.number in page_numbers:
                text_parts.extend(_clean_lines(page.text))
        blocks.append(
            Block(
                kind="OSCE",
                number=number,
                suffix=suffix or None,
                text="\n".join(text_parts).strip(),
                page_numbers=page_numbers,
                images=_images_for_pages(doc, page_numbers),
            )
        )
    return blocks


def segment(doc: ExtractedDocument, kind: str | None = None) -> tuple[str, list[Block]]:
    # "unknown" is what a failed classification stored, not an instruction to
    # classify it that way again. Left as a hint it made the failure permanent:
    # re-ingesting after fixing whatever confused the detector took the stored
    # verdict, skipped detection, and failed identically.
    if kind in (None, "", "unknown"):
        kind = detect_document_kind(doc)
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
