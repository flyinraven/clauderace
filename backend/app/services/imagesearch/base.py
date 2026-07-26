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
    """Raised when a provider cannot complete a search."""
