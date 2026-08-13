"""No fault may exist without something whose job is to close it.

This is the failure that kept repeating. `station_faults` was right about every
image problem this bank has had, and its only consumer was an admin page that
displayed the list. The repairs ran on their own selection criteria - sourcing
picked stations by `image_wanted`, so the questions already showing a blank
screen were the only ones it never looked for - and nobody owned the gap.

The test walks every kind the module can emit and insists it appears in
REMEDIES. Adding a new fault without deciding who repairs it fails here, which
is the only durable guarantee: a checker that finds a problem no one fixes is
how all of this happened.
"""

from __future__ import annotations

import ast
import pathlib

from app.services.osce.repair import REMEDIES

SITTABILITY = (
    pathlib.Path(__file__).resolve().parents[1]
    / "app" / "services" / "osce" / "sittability.py"
)


def fault_kinds_in_source() -> set[str]:
    """Every literal passed as the first argument to Fault(...)."""
    tree = ast.parse(SITTABILITY.read_text(encoding="utf-8"))
    kinds = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Fault"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            kinds.add(node.args[0].value)
    return kinds


def test_every_fault_kind_has_someone_who_repairs_it():
    kinds = fault_kinds_in_source()
    assert kinds, "no Fault kinds found - has sittability moved?"
    orphans = sorted(kinds - set(REMEDIES))
    assert not orphans, (
        f"these faults have no remedy in repair.REMEDIES: {orphans}. "
        f"A fault nobody repairs is found again by the candidate."
    )


def test_no_remedy_names_a_fault_that_cannot_happen():
    """A remedy for a kind that no longer exists is a stale claim of coverage."""
    stale = sorted(set(REMEDIES) - fault_kinds_in_source())
    assert not stale, f"REMEDIES lists kinds sittability never emits: {stale}"


def test_the_repair_order_is_cheapest_first():
    """Reconciling before binding buys words for a question whose picture was
    sitting unclaimed in its own station."""
    source = (
        pathlib.Path(__file__).resolve().parents[1]
        / "app" / "services" / "osce" / "repair.py"
    ).read_text(encoding="utf-8")
    body = source.split("def repair_station", 1)[1].split("@register_handler", 1)[0]
    bind = body.index("bind_ingested_figures_to_questions(db")
    source_at = body.index("source_prompt_images(")
    reconcile = body.index("reconcile_station(db")
    assert bind < source_at < reconcile
