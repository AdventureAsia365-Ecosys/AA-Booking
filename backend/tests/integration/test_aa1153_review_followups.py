"""S291 follow-ups of AA-1153 on a real PostgreSQL (role ``booking``): an exported contract edited and imported back

- is compared with the contract it was exported from even when the DMC holds another published contract starting the
  same day that was published later (a transfers contract): before, the newest one was the diff base, so every rate
  showed as added and the real base's as removed;
- lists its Inventory changes in the diff: an allotment cut from 2 rooms to 1 is a narrowed change, both at the dry
  run and in the live review the second person reads before publishing.

Reuses the AA-1153 part 2 fixtures and helpers. Names are invented."""

from __future__ import annotations

import io

import asyncpg
import pytest
from openpyxl import load_workbook

from tests.integration.test_aa1153_allotment import _allotment_contract
from tests.integration.test_aa1153_hotel_contracts import (  # noqa: F401 — fixture lodge is used by name
    Client,
    _upload,
    lodge,
)
from tests.integration.test_bk020_b2b import (  # noqa: F401 — fixtures admin, client, plan are used by name
    CHECKER,
    MAKER,
    SALES,
    VALID_FROM,
    VALID_TO,
    _ok,
    _staff,
    admin,
    client,
    plan,
)

FIXTURES = (admin, client, plan, lodge)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _units(data: bytes, units: int) -> bytes:
    """The exported sheet with the Units of its Inventory row changed."""
    wb = load_workbook(io.BytesIO(data))
    ws = wb["Inventory"]
    header = [cell.value for cell in ws[1]]
    ws.cell(row=2, column=header.index("Units") + 1, value=units)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


async def test_the_diff_base_is_the_exported_contract_and_a_narrowed_allotment_is_listed(
    client: Client,
    admin: asyncpg.Connection,  # noqa: F811
    lodge: dict[str, str],  # noqa: F811
) -> None:
    c, cis, _, _ = client
    k = await _allotment_contract(c, cis, admin, lodge)
    data = (await c.get(f"/contracts/{k}/rate-sheet", headers=_staff(cis, SALES))).content

    # The same DMC publishes a transfers contract starting the same day, later than the rooms contract.
    extra = await admin.fetchval(
        "INSERT INTO booking.supplier_contracts (supplier_id, title, valid_from, valid_to, currency, created_by)"
        " VALUES ($1::uuid, 'Harbour transfers 2027', $2, $3, 'USD', $4) RETURNING id::text",
        lodge["b"],
        VALID_FROM,
        VALID_TO,
        MAKER,
    )
    await admin.execute(
        "UPDATE booking.supplier_contracts SET status = 'published', published_by = $2,"
        " published_at = now() + interval '1 minute' WHERE id = $1::uuid",
        extra,
        CHECKER,
    )

    maker = _staff(cis, MAKER)
    url = f"/suppliers/{lodge['b']}/rate-sheets"
    same = await _ok(await c.post(f"{url}?dry_run=true", json=_upload(data, "export.xlsx"), headers=maker))
    assert same["diff"]["previous_contract"]["id"] == k  # not the newer transfers contract
    assert (same["diff"]["added"], same["diff"]["changed"], same["diff"]["removed"]) == ([], [], [])
    assert same["diff"]["inventory"] == {"added": [], "changed": [], "removed": [], "narrowed": 0}

    narrowed = _units(data, 1)
    preview = await _ok(await c.post(f"{url}?dry_run=true", json=_upload(narrowed, "export.xlsx"), headers=maker))
    inventory = preview["diff"]["inventory"]
    [change] = inventory["changed"]
    assert inventory["narrowed"] == 1 and change["narrowed"] is True
    assert (change["old"]["units"], change["new"]["units"]) == (2, 1)
    assert change["component_id"] == f"rt:{lodge['room']}" and change["label"]

    # Imported, the live review of the draft names the same base and the same narrowed term.
    done = await _ok(await c.post(url, json=_upload(narrowed, "export.xlsx"), headers=maker))
    draft = done["contract"]["id"]
    stored = await admin.fetchval(
        "SELECT previous_contract_id::text FROM booking.rate_sheet_imports WHERE supplier_contract_id = $1::uuid", draft
    )
    assert stored == k
    review = await _ok(await c.get(f"/contracts/{draft}/review", headers=_staff(cis, CHECKER)))
    assert review["previous_contract"]["id"] == k
    assert review["diff"]["inventory"]["narrowed"] == 1
    assert review["diff"]["inventory"]["changed"][0]["new"]["units"] == 1
