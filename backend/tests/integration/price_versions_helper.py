"""AA-1091 (bk-054): put hand prices live the only way there is, a price list version made by one person and approved
by another (``app.catalog.price_versions``). Shared by the integration tests that need hand prices in a plan's matrix
(they called ``PUT …/prices`` before price versions)."""

from __future__ import annotations

from datetime import date
from typing import Any

import asyncpg

from app.auth.staff import Staff
from app.catalog import price_versions


async def publish_prices(
    conn: asyncpg.Connection,
    source_id: str,
    rows: list[dict[str, Any]],
    *,
    maker: Staff,
    checker: Staff,
    travel_from: date | None = None,
    travel_to: date | None = None,
    below_floor_reason: str | None = None,
    reason: str = "Test prices",
) -> dict[str, Any]:
    """Draft (empty) -> rows -> submit -> approve; returns the published version. Call inside a transaction."""
    first = travel_from or min(r["valid_from"] for r in rows)
    last = travel_to or max(r["valid_to"] for r in rows)
    draft = await price_versions.create_draft(
        conn, maker, source_id, {"copy_from": "none", "travel_from": first, "travel_to": last}
    )
    await price_versions.save_draft(
        conn,
        maker,
        draft["id"],
        {"travel_from": first, "travel_to": last, "items": rows, "below_floor_reason": below_floor_reason},
    )
    submitted = await price_versions.submit(conn, maker, draft["id"], reason)
    return await price_versions.approve(conn, checker, draft["id"], submitted["diff_sha256"])


async def purge_price_versions(admin: asyncpg.Connection, plan_ids: list[Any]) -> None:
    """Delete the price versions of test plans (as bk_owner, inside the caller's transaction, after the plans' matrix
    rows): the rows guard refuses deleting rows of a version that left draft, so it is switched off for the delete."""
    await admin.execute(
        "DELETE FROM booking.trip_plan_prices WHERE version_id IN"
        " (SELECT id FROM booking.price_list_versions WHERE trip_plan_id = ANY($1::uuid[]))",
        plan_ids,
    )
    await admin.execute("ALTER TABLE booking.price_list_version_rows DISABLE TRIGGER price_list_version_rows_guard")
    await admin.execute(
        "DELETE FROM booking.price_list_version_rows WHERE version_id IN"
        " (SELECT id FROM booking.price_list_versions WHERE trip_plan_id = ANY($1::uuid[]))",
        plan_ids,
    )
    await admin.execute("ALTER TABLE booking.price_list_version_rows ENABLE TRIGGER price_list_version_rows_guard")
    await admin.execute("DELETE FROM booking.price_list_versions WHERE trip_plan_id = ANY($1::uuid[])", plan_ids)
