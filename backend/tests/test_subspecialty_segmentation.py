"""The last-resort route for a report headed by subspecialty and duration.

The 2011 Semester 1 report numbers nothing and summarises nothing: each station
is headed "CATARACT" over "9mins". Both traditional routes found nothing, the
classifier called the document unknown, and the upload finished in under a
second having created zero stations.

The point of these tests is as much what the route must NOT do. Every other
paper in the archive segments correctly today and must keep segmenting exactly
the same way, so this route stays shut unless the traditional ones have failed.
"""

from __future__ import annotations

from app.services.ingest.extract import ExtractedDocument, ExtractedPage
from app.services.ingest.segment import (
    detect_document_kind,
    segment,
    subspecialty_heading,
)


def _doc(*pages: str) -> ExtractedDocument:
    return ExtractedDocument(
        pages=[ExtractedPage(number=i + 1, text=t) for i, t in enumerate(pages)],
        page_count=len(pages),
    )


def _station(name: str, body: str = "Please examine this patient.") -> str:
    return f"{name}\n9 mins\n{body}"


# --- the heading itself ---------------------------------------------------
def test_a_subspecialty_over_a_duration_is_a_heading() -> None:
    assert subspecialty_heading("CATARACT\n9mins\nThis 82 yo...") == "Cataract"


def test_the_papers_name_for_a_subspecialty_maps_to_the_banks() -> None:
    """It says RETINA and STRABISMUS where the bank says otherwise."""
    assert subspecialty_heading("RETINA\n9 min\n...") == "Vitreoretinal"
    assert subspecialty_heading("Strabismus\n9 min\n...") == "Ocular Motility"
    assert subspecialty_heading("OCULO-PLASTICS\n4mins each\n...") == "Oculoplastics & Orbit"
    assert subspecialty_heading("CORNEA\nStation 2  4mins\n...") == "Cornea & External Eye"


def test_the_duration_may_sit_mid_line() -> None:
    """Five of the twenty carry it inside a longer line; anchoring missed them."""
    assert subspecialty_heading("NEURO-OPHTHALMOLOGY\nQUESTIONS   9mins\n...")
    assert subspecialty_heading("OCULO-PLASTICS\n2 CASES- 4mins each\n...")


def test_a_subspecialty_named_without_a_duration_is_not_a_heading() -> None:
    """Otherwise body text mentioning the disease would cut the paper up."""
    assert subspecialty_heading("Glaucoma\nis suggested by the disc appearance.") is None


def test_a_duration_without_a_subspecialty_is_not_a_heading() -> None:
    assert subspecialty_heading("Please examine this patient.\n9 mins") is None


# --- classification -------------------------------------------------------
def test_a_report_headed_only_by_subspecialty_is_still_recognised_as_osce() -> None:
    doc = _doc(*[_station(n) for n in ("CATARACT", "GLAUCOMA", "RETINA", "CORNEA")])

    assert detect_document_kind(doc) == "osce"


# --- routing --------------------------------------------------------------
def test_the_paper_is_cut_at_each_heading() -> None:
    doc = _doc(
        _station("CATARACT"), "continued findings",
        _station("GLAUCOMA"),
        _station("RETINA"),
        _station("Strabismus"),
    )

    _, blocks = segment(doc, None)

    assert len(blocks) == 4
    assert [b.number for b in blocks] == [1, 2, 3, 4]
    # The page that carried no heading belongs to the station before it.
    assert blocks[0].page_numbers == [1, 2]


def test_a_paper_that_numbers_its_stations_is_untouched_by_this_route() -> None:
    """The whole rest of the archive. Numbering wins; the headings are ignored."""
    doc = _doc(*[
        f"Station {n}\nSUMMARY OF CASE\nCATARACT\n9 mins\nfindings" for n in range(1, 7)
    ])

    _, blocks = segment(doc, None)

    assert [b.number for b in blocks] == [1, 2, 3, 4, 5, 6]


def test_a_few_stray_station_lines_do_not_shut_the_route_out() -> None:
    """2011 Semester 1 says "Station 2  4mins" inside two of its cases, which
    gave the numbering route two blocks for an eighteen station paper."""
    doc = _doc(
        _station("CATARACT"),
        _station("GLAUCOMA"),
        _station("CORNEA", "Station 2   4mins\nThis lady has a red eye."),
        _station("RETINA"),
        _station("Paediatrics"),
    )

    _, blocks = segment(doc, None)

    assert len(blocks) == 5


def test_a_heading_that_qualifies_itself_is_still_a_heading() -> None:
    """"CORNEA / REFRACTIVE Sx" opens its own station. Missing it merged the
    refractive surgery case into the glaucoma station above it."""
    assert subspecialty_heading("CORNEA / REFRACTIVE Sx\nSTATION 1  4 minutes\n...") \
        == "Cornea & External Eye"


def test_a_sentence_opening_with_a_subspecialty_is_not_a_heading() -> None:
    """The length cap is what keeps the prefix match from cutting up prose."""
    long_line = "Glaucoma is suggested here by the disc appearance and the field loss"
    assert subspecialty_heading(f"{long_line}\n9 mins\n...") is None
