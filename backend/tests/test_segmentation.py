"""Segmentation of OSCE decks, whose slide furniture changes between years.

The failure that motivated these: a deck labelling its stations by subspecialty
and day rather than by number segmented into 3 blocks instead of 18, and the
whole sitting was structured as three enormous stations.
"""

from __future__ import annotations

from app.services.ingest.extract import ExtractedDocument, ExtractedPage
from app.services.ingest.segment import detect_document_kind, segment


def _deck(pages: list[str]) -> ExtractedDocument:
    return ExtractedDocument(
        pages=[ExtractedPage(number=i, text=text) for i, text in enumerate(pages, start=1)],
        page_count=len(pages),
    )


def _numbered_station(number: int) -> list[str]:
    """A deck of the years that put "Station NN" in every footer."""
    return [
        f"Summary of Case\n62F with a red eye\nAim of the Station\nReach a diagnosis\n"
        f"Cornea – Station {number:02d}",
        f"Findings\nInjected conjunctiva\nCornea – Station {number:02d}",
        f"Examiner comments\nMost candidates managed this well.\nCornea – Station {number:02d}",
    ]


def _dated_station(subspecialty: str, day: str) -> list[str]:
    """A deck of the years that label stations by subspecialty and day only."""
    return [
        f"Summary of Case\n55F with dragged discs\nAim of the Station\nFormulate a differential\n"
        f"{subspecialty} - {day}",
        f"Findings\nBilateral scleral buckle\n{subspecialty} - {day}",
        f"Examiner comments\nPoorly answered overall.\n{subspecialty} - {day}",
    ]


COVER = ["RACE OSCE\nSemester 2 2024", "Examiners:\nDr A\nDr B"]


def test_deck_numbered_in_the_footer_splits_per_station() -> None:
    pages = COVER + [p for n in range(1, 5) for p in _numbered_station(n)]
    kind, blocks = segment(_deck(pages))

    assert kind == "osce"
    assert [b.number for b in blocks] == [1, 2, 3, 4]
    assert all("Summary of Case" in b.text for b in blocks)


def test_deck_labelled_by_subspecialty_and_day_still_splits_per_station() -> None:
    pages = COVER + [
        p
        for subspecialty, day in (
            ("Paediatrics", "Thursday"),
            ("Glaucoma", "Thursday"),
            ("Retina", "Friday"),
            ("Uveitis", "Friday"),
        )
        for p in _dated_station(subspecialty, day)
    ]
    doc = _deck(pages)

    assert detect_document_kind(doc) == "osce"
    kind, blocks = segment(doc)

    # No station numbers anywhere in this deck, so position in the deck is the
    # number — and there must be one block per station, not one for the lot.
    assert [b.number for b in blocks] == [1, 2, 3, 4]
    assert "dragged discs" in blocks[0].text
    assert "Uveitis - Friday" in blocks[3].text


def test_a_station_opening_across_two_slides_is_not_split_in_two() -> None:
    pages = COVER + [
        "Summary of Case\n62F with a red eye\nCornea - Thursday",
        "Aim of the Station\nReach a diagnosis\nCornea - Thursday",
        "Findings\nInjected conjunctiva\nCornea - Thursday",
        "Summary of Case\n40M with ptosis\nAim of the Station\nExamine the lids\nOculoplastics - Friday",
        "Findings\nLevator function 4mm\nOculoplastics - Friday",
    ]
    _kind, blocks = segment(_deck(pages))

    assert len(blocks) == 2
    assert "62F with a red eye" in blocks[0].text
    assert "40M with ptosis" in blocks[1].text


def test_a_number_bleeding_across_a_slide_does_not_duplicate_a_station() -> None:
    """Real decks leak the next station's footer onto a trailing slide."""
    pages = COVER + [
        "Summary of Case\n62F red eye\nAim of the Station\nDiagnose\nCornea - Thursday",
        "Findings\nInjected conjunctiva\nCornea - Thursday",
        "Examiner comments\nSee also Station 4\nCornea - Thursday",
        "Summary of Case\n40M ptosis\nAim of the Station\nExamine lids\nOculoplastics - Friday",
        "Findings\nLevator 4mm\nOculoplastics - Friday",
    ]
    _kind, blocks = segment(_deck(pages))

    # The stray "Station 4" must not number the first station 4 and leave two
    # stations indistinguishable downstream.
    assert [b.number for b in blocks] == [1, 2]


def test_written_paper_is_unaffected() -> None:
    pages = [
        "SEQ 1\nQuestion:\nDescribe the management of acute angle closure.\nTotal marks: 10",
        "SEQ 2\nQuestion:\nList four causes of a swollen optic disc.\nTotal marks: 10",
    ]
    doc = _deck(pages)

    assert detect_document_kind(doc) == "written"
    kind, blocks = segment(doc)
    assert kind == "written"
    assert [b.label for b in blocks] == ["SEQ 1", "SEQ 2"]


# --- Page furniture --------------------------------------------------------
def test_a_header_crest_is_not_a_clinical_figure() -> None:
    """Only clinical images belong on a question.

    The examiners' reports carry a college crest at the top of every page and a
    rule at the foot. The repeated-hash rule catches those that are byte
    identical across pages; one re-encoded per page is not, and used to reach a
    station as a figure - and then cost a vision call to say so.
    """
    from app.services.ingest.extract import ExtractedImage, _is_page_furniture

    def img(top: float, bottom: float) -> ExtractedImage:
        return ExtractedImage(
            data=b"x" * 9000, content_type="image/png", width=300, height=200,
            page_number=1, sha256="a" * 64, bbox=(50.0, top, 500.0, bottom),
        )

    page_height = 842.0  # A4 in PDF user space
    assert _is_page_furniture(img(10, 80), page_height), "running header"
    assert _is_page_furniture(img(790, 830), page_height), "running footer"
    assert not _is_page_furniture(img(200, 600), page_height), "a figure in the body"
    # A photograph that starts high but runs down the page is not furniture.
    assert not _is_page_furniture(img(40, 500), page_height)
    # Without placement information nothing can be concluded, so nothing is.
    no_bbox = img(10, 80)
    no_bbox.bbox = None
    assert not _is_page_furniture(no_bbox, page_height)
    assert not _is_page_furniture(img(10, 80), 0)


def _lettered_station(number: int, letter: str) -> list[str]:
    """A deck whose stations are 1A, 1B, 2A ... two stations per number.

    Deliberately without the case markers, so segmentation falls through to the
    station-number route - which is what 2022 Semester 2 does.
    """
    return [
        f"Station {number}{letter}\nRetina\n55M with reduced vision",
        f"Station {number}{letter}\nFindings\nMacular hole",
    ]


def test_a_deck_numbered_1a_1b_is_recognised_as_an_osce() -> None:
    """The letter used to break the pattern outright, scoring zero stations."""
    pages: list[str] = []
    for number in range(1, 10):
        for letter in ("A", "B"):
            pages.extend(_lettered_station(number, letter))
    assert detect_document_kind(_deck(pages)) == "osce"


def test_1a_and_1b_are_two_stations_not_one() -> None:
    pages: list[str] = []
    for number in range(1, 4):
        for letter in ("A", "B"):
            pages.extend(_lettered_station(number, letter))
    _, blocks = segment(_deck(pages))
    assert len(blocks) == 6
    assert [b.printed_number for b in blocks] == ["1A", "1B", "2A", "2B", "3A", "3B"]


def test_an_untitled_slide_belongs_to_the_station_it_follows() -> None:
    """The photographs sit on slides of their own, with no heading at all.

    Keeping only pages that name a station dropped those slides, and with them
    the station's clinical images - which were then re-sourced off the web.
    """
    pages = [
        "Station 1A\nCornea\n40F with a painful eye",
        "",  # a photograph, nothing else on the slide
        "Station 1A\nFindings\nDendritic ulcer",
        "Station 1B\nGlaucoma\n70M for review",
        "",
    ]
    # Named explicitly: five pages is too short a deck to classify on its own,
    # and the question here is how it segments, not how it is recognised.
    _, blocks = segment(_deck(pages), "osce")
    assert len(blocks) == 2
    assert blocks[0].page_numbers == [1, 2, 3]
    assert blocks[1].page_numbers == [4, 5]


def test_a_stored_unknown_does_not_outlive_the_reason_for_it() -> None:
    """Re-ingesting has to classify again, or a fixed detector never gets used.

    2022 Semester 2 failed detection, which stored "unknown" on the document.
    Re-ingest passed that back in as a hint, so the deck failed the same way
    after the pattern that could read it had been fixed.
    """
    pages: list[str] = []
    for number in range(1, 10):
        for letter in ("A", "B"):
            pages.extend(_lettered_station(number, letter))
    kind, blocks = segment(_deck(pages), "unknown")
    assert kind == "osce"
    assert len(blocks) == 18
