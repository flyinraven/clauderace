"""Shared types for clinical image search providers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class ImageCandidate:
    """One search hit, before the bytes have been downloaded."""

    image_url: str
    page_url: str | None = None
    title: str | None = None
    source: str | None = None
    width: int | None = None
    height: int | None = None
    licence: str | None = None
    attribution: str | None = None

    @property
    def provenance(self) -> str:
        parts = [p for p in (self.title, self.source) if p]
        return " — ".join(parts) if parts else (self.page_url or self.image_url)


class ImageSearchProvider(Protocol):
    name: str

    def search(self, query: str, count: int) -> list[ImageCandidate]: ...


class ImageSearchError(RuntimeError):
    """Raised when a provider cannot complete a search.

    Systemic by convention: a missing key, a rejected key, an exhausted credit.
    Every remaining search would fail the same way, so the caller stops.
    """


class ImageQueryError(ImageSearchError):
    """This one query failed; the next one may not.

    A 500 from the provider, a malformed response, a phrase it choked on. Two
    whole batches of station images were abandoned mid-run because Brave
    returned HTTP 500 on a single phrase and the caller could not tell that
    apart from an exhausted account: 35 stations went unsourced over one bad
    query. Subclasses ImageSearchError so existing handlers still catch it.
    """
