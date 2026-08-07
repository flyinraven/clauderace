"""OSCE circuits, station sittings, spoken answers and results.

One 1381-line module, split along the section comments that were already in
it. `stations` is the admin side of the bank, `circuits` is a run of nine of
them, `sittings` is the only part a candidate touches, and `helpers` holds what
more than one of them needs.

The sub-routers carry no prefix of their own; this one does. They are included
in the order the routes were declared in, so nothing that used to be matched by
a literal path can start being swallowed by a parameterised one.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.osce import circuits, sittings, stations

router = APIRouter(prefix="/osce", tags=["osce"])
router.include_router(stations.router)
router.include_router(circuits.router)
router.include_router(sittings.router)
