"""A sourced image is capped before anything holds it.

The byte cap on downloads says nothing about decoded size, and decoded is how
an image is held: a well-compressed JPEG at 10000x8000 is a few megabytes on
the wire and roughly 240 MB as a bitmap. Sourcing runs inside the API process,
opens several candidates per figure and keeps the best across two sweeps, which
is why the OOM kills cluster during the sourcing leg rather than during
extraction.
"""

from __future__ import annotations

import io
import random

import pytest

from app.services.imagesearch.service import (
    MAX_SOURCED_DIMENSION_PX,
    MIN_DIMENSION_PX,
    download_candidate,
)

PILImage = pytest.importorskip("PIL.Image")


class _Candidate:
    def __init__(self, url: str = "https://example.org/photo.jpg") -> None:
        self.image_url = url


def _served(monkeypatch, width: int, height: int, fmt: str = "JPEG") -> None:
    image = PILImage.new("RGB", (width, height))
    pixels = image.load()
    random.seed(3)
    for y in range(0, height, 8):
        for x in range(0, width, 8):
            pixels[x, y] = (random.randint(0, 255), random.randint(0, 255), 0)
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    body = buffer.getvalue()

    class _Response:
        status_code = 200
        headers = {"content-type": f"image/{fmt.lower()}"}
        content = body

    class _Client:
        def __init__(self, *a, **k) -> None: ...
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **k): return _Response()

    monkeypatch.setattr("app.services.imagesearch.service.httpx.Client", _Client)


def test_an_oversized_candidate_is_shrunk_on_arrival(monkeypatch) -> None:
    _served(monkeypatch, 5000, 4000)

    data, content_type, width, height = download_candidate(_Candidate())

    assert max(width, height) == MAX_SOURCED_DIMENSION_PX
    assert (width, height) == (2000, 1600), "aspect ratio must survive"
    assert content_type == "image/jpeg"
    with PILImage.open(io.BytesIO(data)) as stored:
        assert stored.size == (2000, 1600), "the bytes returned must match the size reported"


def test_a_candidate_within_the_cap_is_returned_untouched(monkeypatch) -> None:
    _served(monkeypatch, 1200, 900)

    data, _, width, height = download_candidate(_Candidate())

    assert (width, height) == (1200, 900)
    with PILImage.open(io.BytesIO(data)) as stored:
        assert stored.size == (1200, 900)


def test_a_candidate_below_the_floor_is_still_rejected(monkeypatch) -> None:
    """The existing minimum must keep working - a thumbnail is not a figure."""
    _served(monkeypatch, MIN_DIMENSION_PX - 50, MIN_DIMENSION_PX - 50)

    assert download_candidate(_Candidate()) is None
