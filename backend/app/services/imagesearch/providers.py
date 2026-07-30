"""Image search provider adapters.

Google's Custom Search JSON API is closed to new customers and retires on
1 January 2027, and Microsoft retired the Bing Search API in August 2025, so
neither is a viable default. Brave is the broad-web option; Openverse is a
free, key-less fallback restricted to openly licensed material.
"""

from __future__ import annotations

import logging
import re

import httpx

from app.services.coerce import as_int
from app.services.imagesearch.base import (
    ImageCandidate,
    ImageQueryError,
    ImageSearchError,
)

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 20

# Brave accepts 400 characters and 50 words. Long before either, a phrase built
# from a station's whole diagnosis stops being a search and starts being a
# sentence, and the results get worse rather than better.
MAX_QUERY_CHARS = 160


def tidy_query(query: str) -> str:
    """What to actually send: no trailing full stop, no brackets, not too long.

    Brave answered HTTP 500 to "nine positions of gaze photograph Congenital
    fibrosis of extraocular muscles." - seventy-seven characters, and the only
    thing unusual about it was the full stop the rubric ended on.
    """
    text = re.sub(r"[<>\[\]{}|\^~]", " ", query or "")
    text = re.sub(r"\(.*?\)", ' ', text)
    text = re.sub(r"\s+", " ", text).strip(" .,;:-")
    if len(text) <= MAX_QUERY_CHARS:
        return text
    cut = text[:MAX_QUERY_CHARS].rsplit(" ", 1)[0]
    return cut.strip(" .,;:-")


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
            "q": tidy_query(query),
            "count": max(1, min(count, 50)),
            "safesearch": "strict",
        }
        try:
            with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
                response = client.get(self.ENDPOINT, headers=headers, params=params)
        except httpx.HTTPError as exc:
            # A dropped connection says nothing about the next phrase.
            raise ImageQueryError(f"Brave request failed: {exc}") from exc

        # Only the account-level answers stop the run. Anything else is this
        # query, and the next one deserves its turn.
        if response.status_code in (401, 403):
            raise ImageSearchError(f"Brave rejected the API key ({response.status_code}).")
        if response.status_code in (402, 429):
            raise ImageSearchError(
                f"Brave rate limit or credit exhausted ({response.status_code})."
            )
        if response.status_code >= 400:
            raise ImageQueryError(
                f"Brave returned HTTP {response.status_code}: {response.text[:300]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise ImageQueryError("Brave returned a non-JSON response") from exc

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
                    width=as_int(properties.get("width") or item.get("width")),
                    height=as_int(properties.get("height") or item.get("height")),
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
            raise ImageQueryError(f"Openverse request failed: {exc}") from exc

        if response.status_code >= 400:
            raise ImageQueryError(f"Openverse returned HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise ImageQueryError("Openverse returned a non-JSON response") from exc

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
                    width=as_int(item.get("width")),
                    height=as_int(item.get("height")),
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
            raise ImageQueryError(f"SerpAPI request failed: {exc}") from exc

        if response.status_code >= 400:
            raise ImageQueryError(
                f"SerpAPI returned HTTP {response.status_code}: {response.text[:200]}"
            )

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
                    width=as_int(item.get("original_width")),
                    height=as_int(item.get("original_height")),
                )
            )
        return candidates
