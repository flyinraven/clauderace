"""Typesetter glyphs must not survive into question text.

The RANZCO PDFs carry ligature glyphs in their text layer, so "a useful
negative finding" extracted as "a useful negative \ufb01nding". It renders
acceptably, which is why it went unnoticed, and then no search for "finding"
matches it and the grading model is handed a word it has to guess at.
"""

from __future__ import annotations

import pytest

from app.services.ingest.extract import ExtractedPage, normalise_extracted_text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a useful negative \ufb01nding", "a useful negative finding"),
        ("what in o\ufb03ce tests", "what in office tests"),
        ("Brie\ufb02y describe", "Briefly describe"),
        ("sta\ufb00 grade", "staff grade"),
        ("wa\ufb04e", "waffle"),
    ],
)
def test_ligatures_become_the_letters_they_stand_for(raw, expected):
    assert normalise_extracted_text(raw) == expected


def test_smart_punctuation_is_folded_for_searchability():
    assert normalise_extracted_text("don\u2019t \u201cquote\u201d me") == "don't \"quote\" me"


def test_invisible_separators_are_removed():
    assert normalise_extracted_text("hypo\u00adtony") == "hypotony"
    assert normalise_extracted_text("6/9\u200b.6") == "6/9.6"
    assert normalise_extracted_text("IOP\u00a04") == "IOP 4"


def test_clinical_notation_is_left_alone():
    """A blanket NFKC pass would rewrite these; the targeted map must not."""
    for text in ("30 \u00b5m", "\u00bd tablet", "20/20", "PO\u2082", "-7.25 DS x 180"):
        assert normalise_extracted_text(text) == text


def test_every_extractor_gets_it_because_the_page_normalises_itself():
    assert ExtractedPage(number=1, text="o\ufb03ce").text == "office"


def test_empty_text_is_not_an_error():
    assert normalise_extracted_text("") == ""
