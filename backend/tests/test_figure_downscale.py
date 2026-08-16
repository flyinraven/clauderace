"""Oversized figures are shrunk on extraction, and only when that helps.

The reason this exists is memory, not storage: the extractor holds every figure
in a document at once, and the block cache then pins them for the length of the
job, so a paper full of press-resolution photographs took the container down.
The tests that matter are therefore the ones about what is *not* shrunk - a
figure the candidate has to read closely is worth more than the megabytes.
"""

from __future__ import annotations

import io
import random

import pytest

from app.services.ingest.extract import (
    DOWNSCALE_ABOVE_BYTES,
    MAX_DIMENSION_PX,
    downscale_oversized,
)

PILImage = pytest.importorskip("PIL.Image")
PILDraw = pytest.importorskip("PIL.ImageDraw")


def _photograph(width: int, height: int, fmt: str = "JPEG") -> bytes:
    """Something with enough detail that it will not trivially compress."""
    image = PILImage.new("RGB", (width, height))
    pixels = image.load()
    random.seed(7)
    for y in range(0, height, 3):
        for x in range(0, width, 3):
            pixels[x, y] = (
                random.randint(0, 255), random.randint(0, 255), random.randint(0, 255)
            )
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    return buffer.getvalue()


def test_a_press_resolution_photograph_is_capped() -> None:
    original = _photograph(4000, 3000)
    shrunk, content_type, width, height = downscale_oversized(
        original, "image/jpeg", 4000, 3000
    )

    assert max(width, height) == MAX_DIMENSION_PX
    assert (width, height) == (2000, 1500), "aspect ratio must be preserved"
    assert len(shrunk) < len(original)
    assert content_type == "image/jpeg"


def test_a_figure_within_the_cap_is_left_exactly_as_it_was() -> None:
    original = _photograph(1600, 1200)
    shrunk, content_type, width, height = downscale_oversized(
        original, "image/jpeg", 1600, 1200
    )

    assert shrunk is original
    assert (content_type, width, height) == ("image/jpeg", 1600, 1200)


def test_a_small_file_is_not_re_encoded_however_many_pixels_it_claims() -> None:
    """Line art is enormous in pixels and tiny in bytes; re-encoding only loses."""
    image = PILImage.new("RGB", (3000, 2400), "white")
    draw = PILDraw.Draw(image)
    for x in range(0, 3000, 60):
        draw.line([(x, 0), (x, 2400)], fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    original = buffer.getvalue()
    assert len(original) < DOWNSCALE_ABOVE_BYTES

    shrunk, _, width, height = downscale_oversized(original, "image/png", 3000, 2400)

    assert shrunk is original
    assert (width, height) == (3000, 2400)


def test_line_art_stays_png_rather_than_becoming_jpeg() -> None:
    """JPEG ringing around high-contrast edges is what makes a decibel unreadable."""
    original = _photograph(3000, 2400, fmt="PNG")

    _, content_type, _, _ = downscale_oversized(original, "image/png", 3000, 2400)

    assert content_type == "image/png"


def test_a_re_encode_that_gains_nothing_keeps_the_original() -> None:
    """Noise re-encodes larger as PNG. The original is then both smaller and lossless."""
    original = _photograph(3000, 2400, fmt="PNG")
    assert len(original) > DOWNSCALE_ABOVE_BYTES

    shrunk, _, width, height = downscale_oversized(original, "image/png", 3000, 2400)

    if shrunk is not original:
        assert len(shrunk) < len(original)
    else:
        assert (width, height) == (3000, 2400)


def test_an_unreadable_figure_is_passed_through_not_dropped() -> None:
    """A decoder failure must not cost a station its image, nor its dimensions:
    a figure recorded as 0x0 is discarded downstream as a logo."""
    shrunk, content_type, width, height = downscale_oversized(
        b"not an image at all", "image/png", 5000, 4000
    )

    assert shrunk == b"not an image at all"
    assert (content_type, width, height) == ("image/png", 5000, 4000)


def test_shrinking_is_deterministic_so_repeated_templates_still_collapse() -> None:
    """Hashing happens after shrinking; a letterhead on ninety pages must still
    produce ninety identical hashes, or the decorative filter stops firing."""
    original = _photograph(4000, 3000)

    first = downscale_oversized(original, "image/jpeg", 4000, 3000)[0]
    second = downscale_oversized(original, "image/jpeg", 4000, 3000)[0]

    assert first == second
