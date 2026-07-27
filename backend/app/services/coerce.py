"""Coercion of model output.

A model returns "3" where an integer was asked for, or omits the field, or
sends null. Every caller that reads a number out of a JSON reply needs the same
forgiving conversion, so it lives here rather than at the bottom of six modules.
"""

from __future__ import annotations

from typing import Any


def as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_optional_float(value: Any) -> float | None:
    """For fields where absent and zero mean different things - an Angoff
    expectation of 0.0 is a claim, a missing one is not."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def clean_str(value: Any) -> str | None:
    """Trimmed text, or None for anything empty or absent."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None
