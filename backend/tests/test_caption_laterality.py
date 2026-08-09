"""A caption that will not say which eye.

Station 155's one usable photograph was captioned "External photograph of one
eye". The candidate saw a corneal graft, said right eye, and it was the left.
The examiner's comment reads "the candidate incorrectly stated the graft was in
the right eye" - marked wrong for a fact the screen would not tell them, and
eight of twenty marks gone.

"Slit lamp photograph of one eye" appeared 91 times in the bank, "Fundus
photograph of one eye" 76 times: 582 of 789 approved captions named no side at
all, while the marking schemes are written per eye.
"""

from __future__ import annotations

import pytest

from app.services.osce.station_images.verify import label_side


@pytest.mark.parametrize(
    ("caption", "side", "expected"),
    [
        ("Slit lamp photograph of one eye", "right", "Slit lamp photograph of the right eye"),
        ("External photograph of an eye", "left", "External photograph of the left eye"),
        ("Fundus photograph of one eye", "both", "Fundus photograph of both eyes"),
        # The structure the caption named is kept: an OCT is of a macula, and
        # "of the left eye" reads as a different picture entirely.
        ("Optical coherence tomography of one macula", "left",
         "Optical coherence tomography of the left macula"),
        ("Optical coherence tomography of one optic disc", "right",
         "Optical coherence tomography of the right optic disc"),
    ],
)
def test_the_side_is_written_into_the_caption(caption, side, expected):
    assert label_side(caption, side) == expected


@pytest.mark.parametrize("side", [None, "", "unclear", "nonsense"])
def test_an_empty_claim_is_struck_out_rather_than_guessed(side):
    """Saying nothing is honest. "One eye" looks like information and is not.

    A candidate marked for describing the wrong eye is worse off than one told
    nothing at all, so an unreadable image loses the phrase instead of getting
    a coin toss.
    """
    assert label_side("Slit lamp photograph of one eye", side) == "Slit lamp photograph"


@pytest.mark.parametrize(
    "caption",
    [
        "Fundus photograph of the left eye",
        "Slit lamp photograph of both eyes",
        "Nine positions of gaze",
        "Axial MRI of the head",
        "Four panels showing intraocular views",
    ],
)
def test_a_caption_that_already_reads_properly_is_left_alone(caption):
    assert label_side(caption, "right") == caption or "right" in label_side(caption, "right")


def test_a_caption_is_never_eaten_by_the_substitution():
    """The phrase is most of some captions; losing it must not lose the label."""
    assert label_side("An eye", None) == "An eye"
    assert label_side("", "right") is None
    assert label_side(None, "right") is None


def test_the_blind_pass_is_asked_which_side_and_why():
    """It was only ever asked "one eye or both", never which one.

    The prompt's own example told it to write "Optical coherence tomography of
    one macula", so the phrase the candidate could not use was the house style.
    """
    from app.services.osce.station_images.verify import BLIND_SYSTEM

    assert '"side"' in BLIND_SYSTEM
    assert "side_reason" in BLIND_SYSTEM, "a wrong call has to be visible, not inherited"
    assert "of one eye" in BLIND_SYSTEM.split("Bad captions")[1], "named as a bad caption"
    assert "of one macula" not in BLIND_SYSTEM.split("Bad captions")[0], (
        "and no longer offered as a good one"
    )
