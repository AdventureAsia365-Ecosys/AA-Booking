"""AA-1175 (bk-066) on a real PostgreSQL, as role ``booking``: the group-size bands of the computed price matrix are
the pricing policy parameter ``price_pax_bands``.

The seed has two versions (the bands the code used, then the business owner's bands); a change is validated (from 1,
no gap or overlap, at most 8 bands), needs a second person and is audited; a recompute then prices the plan's matrix
on the bands in force, and a party of 4, 6 or 9 finds the row of its band. The test changes the bands back to the
seed, so the other modules keep the seeded bands. Staff ids, the supplier and the tour are invented (test_bk019_b2b).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.pricing import pax_bands, policy_values
from tests.integration.test_bk019_b2b import (  # noqa: F401 — fixtures used by name
    CHECKER,
    MAKER,
    SALES,
    SOURCE_ID,
    _ok,
    _staff,
    admin,
    client,
    supply,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

KEY = pax_bands.KEY
OLD = [[1, 1], [2, 2], [3, 4], [5, 6], [7, 10], [11, None]]
SEED = [[1, 1], [2, 2], [3, 3], [4, 4], [5, 7], [8, 10], [11, None]]
WIDER = [[1, 1], [2, 2], [3, 5], [6, None]]
COVERING = (
    "SELECT pr.pax_min, pr.pax_max FROM booking.trip_plan_prices pr JOIN booking.trip_plans p ON p.id = pr.trip_plan_id"
    " WHERE p.source_id = $1 AND pr.computed AND pr.channel = 'b2b' AND pr.pax_min <= $2"
    " AND (pr.pax_max IS NULL OR pr.pax_max >= $2) ORDER BY pr.valid_from LIMIT 1"
)
BANDS = (
    "SELECT DISTINCT pr.pax_min, pr.pax_max FROM booking.trip_plan_prices pr"
    " JOIN booking.trip_plans p ON p.id = pr.trip_plan_id WHERE p.source_id = $1 AND pr.computed ORDER BY 1"
)


@pytest_asyncio.fixture
async def matrix_clean(admin: asyncpg.Connection) -> AsyncIterator[None]:  # noqa: F811
    """The plan's matrix and its dated history (test_bk019_b2b's clean-up deletes the plan, which they reference)."""
    yield
    for table in ("trip_plan_price_history", "trip_plan_prices"):
        await admin.execute(
            f"DELETE FROM booking.{table} WHERE trip_plan_id IN"  # noqa: S608 — fixed table names
            " (SELECT id FROM booking.trip_plans WHERE source_id = $1)",
            SOURCE_ID,
        )


def _item(listing: dict, key: str) -> dict:
    return next(i for i in listing["items"] if i["key"] == key)


async def _bands(admin: asyncpg.Connection) -> list[tuple[int, int | None]]:  # noqa: F811
    return [(r["pax_min"], r["pax_max"]) for r in await admin.fetch(BANDS, SOURCE_ID)]


async def _set_bands(c: httpx.AsyncClient, h: dict, value: list, reason: str) -> dict:
    today = datetime.now(UTC).date().isoformat()
    body = {"key": KEY, "value": value, "effective_from": today, "reason": reason}
    version = await _ok(await c.post("/pricing/policy", json=body, headers=h[MAKER]), 201)
    return await _ok(await c.post(f"/pricing/policy/{version['id']}/approve", headers=h[CHECKER]))


async def test_the_seeded_bands_price_the_matrix_and_a_change_needs_a_second_person(
    client: tuple[httpx.AsyncClient, object, object],  # noqa: F811
    admin: asyncpg.Connection,  # noqa: F811
    supply: dict[str, str],  # noqa: F811
    matrix_clean: None,
) -> None:
    c, cis, _ = client
    h = {who: _staff(cis, who) for who in (MAKER, CHECKER, SALES)}  # type: ignore[arg-type]
    today = datetime.now(UTC).date()

    # 1. The seed: v2 (the business owner's bands) in force, replacing v1 (the bands the code used).
    item = _item(await _ok(await c.get("/pricing/policy", headers=h[SALES])), KEY)
    assert (item["type"], item["unit"], item["section"], item["approver"]) == (
        "pax_bands",
        "travellers",
        "prices",
        "pricing",
    )
    assert item["current"]["value"] == SEED and item["current"]["version_no"] == 2
    assert item["current"]["created_by"]["id"] == "seed:bk-066" and item["current"]["diff"] == {"from": OLD, "to": SEED}
    assert [(v["version_no"], v["value"], v["status"]) for v in item["history"]] == [
        (2, SEED, "in_force"),
        (1, OLD, "superseded"),
    ]
    assert item["guide"]["en"]["example"].startswith("Starting value 1, 2, 3, 4, 5-7, 8-10, 11+")
    assert (await policy_values.load(admin, today)).pax_bands == pax_bands.as_bands(SEED)

    # 2. The plan's matrix on the seeded bands; parties of 4, 6 and 9 find the row of their band.
    commercial = {"sellable": False, "price_hint": {"amount": "150.00", "currency": "USD", "basis": "per_person"}}
    await _ok(await c.put(f"/catalog/trip-plans/{SOURCE_ID}/commercial", json=commercial, headers=h[MAKER]))
    items = {
        "items": [{"day_no": 1, "hotel_room_type_id": supply["room"]}, {"day_no": 1, "service_id": supply["service"]}]
    }
    await _ok(await c.put(f"/catalog/trip-plans/{SOURCE_ID}/components", json=items, headers=h[MAKER]))
    await _ok(await c.post(f"/catalog/trip-plans/{SOURCE_ID}/prices/recompute", headers=h[MAKER]))
    assert await _bands(admin) == [tuple(b) for b in SEED]
    for party, band in ((4, (4, 4)), (6, (5, 7)), (9, (8, 10)), (12, (11, None))):
        row = await admin.fetchrow(COVERING, SOURCE_ID, party)
        assert (row["pax_min"], row["pax_max"]) == band, party

    # 3. Invalid bands are refused with the reason; nothing is written.
    base = {"key": KEY, "effective_from": today.isoformat(), "reason": "Wider group bands"}
    for bad, says in (
        ([[1, 1], [3, None]], "band 2 must start at 2"),  # a gap: 2 travellers in no band
        ([[1, 2], [2, None]], "band 2 must start at 3"),  # an overlap
        ([[n, n] for n in range(1, 9)] + [[9, None]], "a list of 1 to 8 bands"),
        ([[1, None], [2, 4]], "only the last band may be open"),
        ([[1, 61]], "at most 60"),
    ):
        refused = await c.post("/pricing/policy", json={**base, "value": bad}, headers=h[MAKER])
        assert refused.status_code == 422 and refused.json()["detail"] == "invalid_value", refused.text
        assert says in refused.text, (bad, refused.text)

    # 4. A valid change: pending, never self-approved, approved by a second person, audited; the matrix moves only at
    # the next recompute.
    try:
        version = await _ok(await c.post("/pricing/policy", json={**base, "value": WIDER}, headers=h[MAKER]), 201)
        assert version["status"] == "pending" and version["diff"] == {"from": SEED, "to": WIDER}
        own = await c.post(f"/pricing/policy/{version['id']}/approve", headers=h[MAKER])
        assert own.status_code == 403 and own.json()["detail"] == "own_row"
        approved = await _ok(await c.post(f"/pricing/policy/{version['id']}/approve", headers=h[CHECKER]))
        assert approved["status"] == "in_force"
        audit = await admin.fetch(
            "SELECT action FROM booking.audit_logs WHERE entity_type = 'pricing_policy' AND entity_id = $1::uuid",
            version["id"],
        )
        assert sorted(r["action"] for r in audit) == ["pricing_policy.approved", "pricing_policy.submitted"]
        assert await _bands(admin) == [tuple(b) for b in SEED]  # computed rows change only at the next recompute
        await _ok(await c.post(f"/catalog/trip-plans/{SOURCE_ID}/prices/recompute", headers=h[MAKER]))
        assert await _bands(admin) == [tuple(b) for b in WIDER]
        row = await admin.fetchrow(COVERING, SOURCE_ID, 4)
        assert (row["pax_min"], row["pax_max"]) == (3, 5)
    finally:
        # Back to the seed (a new version; the later approval of the same day wins).
        current = (await policy_values.load(admin, today)).values.get(KEY)
        if current != SEED:
            await _set_bands(c, h, SEED, "Back to the seeded group-size bands")
    assert (await policy_values.load(admin, today)).pax_bands == pax_bands.as_bands(SEED)
