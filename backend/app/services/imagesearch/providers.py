"""Image search provider adapters.

Google's Custom Search JSON API is closed to new customers and retires on
1 January 2027, and Microsoft retired the Bing Search API in August 2025, so
neither is a viable default. Brave is the broad-web option; Openverse is a
free, key-less fallback restricted to openly licensed material.
"""

from __future__ import annotations

import logging

import httpx

from app.services.imagesearch.base import ImageCandidate, ImageSearchError

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 20


class BraveImageSearch:
    """Brave Search API - images endpoint.

    Billing note: Brave's free allowance is a monthly credit rather than a hard
    cap, and overages are charged. The caller is expected to enforce
    `imagesearch.monthly_query_limit` before calling this.
    """

    name = "brave"
    ENDPOINT = "https://api.search.brave.com/res/v1/images/search"

    def __init__(self, api_key: str):
        if not api_key:
            raise ImageSearchError("Brave Search API key is not set")
        self.api_key = api_key

    def search(self, query: str, count: int) -> list[ImageCandidate]:
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self.api_key,
        }
        params = {
            "q": query,
            "count": max(1, min(count, 50)),
            "safesearch": "strict",
        }
        try:
            with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
                response = client.get(self.ENDPOINT, headers=headers, params=params)
        except httpx.HTTPError as exc:
            raise ImageSearchError(f"Brave request failed: {exc}") from exc

        if response.status_code == 401:
            raise ImageSearchError("Brave rejected the API key (401).")
        if response.status_code == 429:
            raise ImageSearchError("Brave rate limit or credit exhausted (429).")
        if response.status_code >= 400:
            raise ImageSearchError(f"Brave returned HTTP {response.status_code}: {response.text[:300]}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise ImageSearchError("Brave returned a non-JSON response") from exc

        candidates: list[ImageCandidate] = []
        for item in payload.get("results") or []:
            properties = item.get("properties") or {}
            image_url = properties.get("url") or (item.get("thumbnail") or {}).get("src")
            if not image_url:
                continue
            candidates.append(
                ImageCandidate(
                    image_url=image_url,
                    page_url=item.get("url"),
                    title=item.get("title"),
                    source=item.get("source") or (item.get("meta_url") or {}).get("hostname"),
                    width=_as_int(properties.get("width") or item.get("width")),
                    height=_as_int(properties.get("height") or item.get("height")),
                )
            )
        return candidates


class OpenverseImageSearch:
    """Openverse - openly licensed images, no API key required.

    Far narrower than a web search for clinical photographs, but everything it
    returns carries an explicit licence, which makes it the safe default when
    an administrator has not configured a paid provider.
    """

    name = "openverse"
    ENDPOINT = "https://api.openverse.org/v1/images/"

    def search(self, query: str, count: int) -> list[ImageCandidate]:
        params = {
            "q": query,
            "page_size": max(1, min(count, 20)),
            "mature": "false",
            # Exclude licences that forbid derivative use outright.
            "license_type": "all-cc",
        }
        try:
            with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
                response = client.get(
                    self.ENDPOINT,
                    params=params,
                    headers={"User-Agent": "RACE-Exam-Simulator/0.1 (education)"},
                )
        except httpx.HTTPError as exc:
            raise ImageSearchError(f"Openverse request failed: {exc}") from exc

        if response.status_code >= 400:
            raise ImageSearchError(f"Openverse returned HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise ImageSearchError("Openverse returned a non-JSON response") from exc

        candidates: list[ImageCandidate] = []
        for item in payload.get("results") or []:
            image_url = item.get("url")
            if not image_url:
                continue
            licence = item.get("license")
            version = item.get("license_version")
            candidates.append(
                ImageCandidate(
                    image_url=image_url,
                    page_url=item.get("foreign_landing_url"),
                    title=item.get("title"),
                    source=item.get("source"),
                    width=_as_int(item.get("width")),
                    height=_as_int(item.get("height")),
                    licence=f"{licence.upper()} {version}".strip() if licence else None,
                    attribution=item.get("attribution"),
                )
            )
        return candidates


class SerpApiImageSearch:
    """SerpAPI - Google Images results. Paid, highest quality."""

    name = "serpapi"
    ENDPOINT = "https://serpapi.com/search"

    def __init__(self, api_key: str):
        if not api_key:
            raise ImageSearchError("SerpAPI key is not set")
        self.api_key = api_key

    def search(self, query: str, count: int) -> list[ImageCandidate]:
        params = {
            "engine": "google_images",
            "q": query,
            "api_key": self.api_key,
            "safe": "active",
            "num": max(1, min(count, 100)),
        }
        try:
            with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
                response = client.get(self.ENDPOINT, params=params)
        except httpx.HTTPError as exc:
            raise ImageSearchError(f"SerpAPI request failed: {exc}") from exc

        if response.status_code >= 400:
            raise ImageSearchError(f"SerpAPI returned HTTP {response.status_code}: {response.text[:200]}")

        payload = response.json()
        if payload.get("error"):
            raise ImageSearchError(f"SerpAPI: {payload['error']}")

        candidates: list[ImageCandidate] = []
        for item in payload.get("images_results") or []:
            image_url = item.get("original") or item.get("thumbnail")
            if not image_url:
                continue
            candidates.append(
                ImageCandidate(
                    image_url=image_url,
                    page_url=item.get("link"),
                    title=item.get("title"),
                    source=item.get("source"),
                    width=_as_int(item.get("original_width")),
                    height=_as_int(item.get("original_height")),
                )
            )
        return candidates


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
