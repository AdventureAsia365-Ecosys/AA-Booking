"""AA-1088 M13 follow-up (S278 lane l18): the display fix of B2B cases confirmed before #163 / #231
(:mod:`app.b2b.legacy_cases`) and the bk-056 lock exception it relies on, as role ``booking`` on a real PostgreSQL.

A staff-entered booking of the bk-020 plan is confirmed today, then turned into what an old confirm wrote (as
``bk_owner`` with the row guards off for the set-up only): placeholder version and item titles, end date = start date,
item ``day_to`` = ``day_from``; the catalog tour is 4 days. Covers: the DRY RUN counts and writes nothing; ``apply``
writes the tour title, the end date start + 3 and the item span, with one audit row; a second plan finds nothing; the
locks still refuse the same change without the setting, any other column with it, and a title that is not the
placeholder.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import asyncpg
import httpx
import pytest

from app.b2b import legacy_cases
from app.db import Database
from tests.integration.test_aa1075_booking_ops import bk047  # noqa: F401 — fixture used by name
from tests.integration.test_bk020_b2b import (  # noqa: F401 — fixtures admin, client, plan are used by name
    DAY1,
    SALES,
    SOURCE_ID,
    _departure,
    _ok,
    _plan_line,
    _staff,
    admin,
    client,
    plan,
)
from tests.staff_tokens import LocalCis, LocalM2M

FIXTURES = (admin, bk047, client, plan)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

Client = tuple[httpx.AsyncClient, LocalCis, LocalM2M, Database]
LOCKED = ("booking.trip_case_versions", "booking.trip_case_version_itinerary")
LOCK_ERROR = "approved and locked"


async def _legacy_case(admin: asyncpg.Connection, client: Client, plan: dict[str, str]) -> dict[str, Any]:
    c, cis, _, _ = client
    dep = await _departure(c, cis, DAY1, 10)
    entry = {
        "partner_id": plan["partner"],
        "agency_ref": "AG-L18-OLD",
        "currency": "USD",
        "lines": [_plan_line("tour", plan["plan"], dep["id"])],
    }
    made = await _ok(
        await c.post("/b2b-desk/entries", json=entry, headers={**_staff(cis, SALES), "Idempotency-Key": "l18-old-1"}),
        201,
    )
    case_id = made["trip_case"]["id"]
    hold = await admin.fetchrow(
        "SELECT h.proposal_ref, h.proposal_version, ql.component_kind, ql.component_id, ql.line_ref"
        " FROM booking.b2b_holds h JOIN booking.b2b_quote_lines ql ON ql.quote_id = h.quote_id AND ql.line_no = 1"
        " WHERE h.trip_case_id = $1::uuid",
        case_id,
    )
    version_title = f"B2B {hold['proposal_ref']} v{hold['proposal_version']}"
    item_title = f"{hold['component_kind']} {hold['component_id']} · line {hold['line_ref']}"
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE bk_owner")
        for t in LOCKED:
            await admin.execute(f"ALTER TABLE {t} DISABLE TRIGGER USER")
        await admin.execute(
            "UPDATE booking.trip_case_versions SET title = $2 WHERE trip_case_id = $1::uuid", case_id, version_title
        )
        await admin.execute(
            "UPDATE booking.trip_case_version_itinerary i SET title = $2, day_to = i.day_from"
            " FROM booking.trip_case_versions v WHERE v.id = i.trip_case_version_id AND v.trip_case_id = $1::uuid",
            case_id,
            item_title,
        )
        await admin.execute("UPDATE booking.trip_cases SET end_date = start_date WHERE id = $1::uuid", case_id)
        for t in LOCKED:
            await admin.execute(f"ALTER TABLE {t} ENABLE TRIGGER USER")
    await admin.execute("UPDATE catalog.v_tour SET duration_days = 4, title = 'Lantern Bay 4D' WHERE source_id = $1",
                        SOURCE_ID)  # fmt: skip
    return {"case_id": case_id, "code": made["trip_case"]["code"], "start": date.fromisoformat(DAY1)}


async def _state(conn: asyncpg.Connection, case_id: str) -> dict[str, Any]:
    row = await conn.fetchrow(
        "SELECT tc.end_date, v.title AS version_title, i.title AS item_title, i.day_from, i.day_to"
        " FROM booking.trip_cases tc JOIN booking.trip_case_versions v ON v.trip_case_id = tc.id"
        " JOIN booking.trip_case_version_itinerary i ON i.trip_case_version_id = v.id WHERE tc.id = $1::uuid",
        case_id,
    )
    return dict(row)


async def test_plan_then_apply_then_nothing_left(
    admin: asyncpg.Connection, client: Client, plan: dict[str, str], bk047: None, migrated_booking_dsn: str
) -> None:
    case = await _legacy_case(admin, client, plan)
    conn = await asyncpg.connect(migrated_booking_dsn)
    try:
        before = await _state(conn, case["case_id"])
        assert before["end_date"] == case["start"] and before["day_to"] == before["day_from"]
        planned = await legacy_cases.plan(conn)
        assert (planned["to_fix"], planned["end_dates"], planned["version_titles"]) == (1, 1, 1)
        assert (planned["item_titles"], planned["item_spans"]) == (1, 1)
        assert await _state(conn, case["case_id"]) == before  # DRY RUN wrote nothing

        await legacy_cases.apply(conn)
        after = await _state(conn, case["case_id"])
        assert after["end_date"] == case["start"] + timedelta(days=3)
        assert after["version_title"] == "Lantern Bay 4D" and after["item_title"] == "Lantern Bay 4D"
        assert after["day_to"] == after["day_from"] + 3
        audit = await admin.fetchrow(
            "SELECT actor_type, actor_ref FROM booking.audit_logs WHERE action = 'trip_case.legacy_display_fixed'"
            " AND entity_id = $1::uuid",
            case["case_id"],
        )
        assert dict(audit) == {"actor_type": "system", "actor_ref": legacy_cases.ACTOR}
        again = await legacy_cases.plan(conn)
        assert again["to_fix"] == 0
        report = legacy_cases._report(planned)
        assert report["cases"] == [{"code": case["code"], "end_date": [case["start"].isoformat(),
                                    (case["start"] + timedelta(days=3)).isoformat()], "days_added": 3,
                                    "version_title": True, "items": 1}]  # fmt: skip
    finally:
        await conn.close()


async def test_the_locks_open_only_for_the_fix(
    admin: asyncpg.Connection, client: Client, plan: dict[str, str], bk047: None, migrated_booking_dsn: str
) -> None:
    case = await _legacy_case(admin, client, plan)
    conn = await asyncpg.connect(migrated_booking_dsn)
    version = "SELECT id FROM booking.trip_case_versions WHERE trip_case_id = $1::uuid"
    try:
        # Without the setting: refused as before.
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match=LOCK_ERROR):
            await conn.execute(
                f"UPDATE booking.trip_case_versions SET title = 'Anything' WHERE id = ({version})",  # noqa: S608
                case["case_id"],
            )
        for sql in (
            # another column than the title
            f"UPDATE booking.trip_case_versions SET title = 'Lantern Bay 4D', total_price = 1 WHERE id = ({version})",
            # an item column other than title / day_to
            "UPDATE booking.trip_case_version_itinerary SET day_from = day_from + 1 WHERE trip_case_version_id ="
            f" ({version})",
            # a shorter span
            "UPDATE booking.trip_case_version_itinerary SET day_to = day_to - 1 WHERE trip_case_version_id ="
            f" ({version})",
        ):
            async with conn.transaction():
                await conn.execute("SELECT set_config('booking.legacy_display_fix', 'aa1088', true)")
                with pytest.raises(asyncpg.PostgresError):
                    async with conn.transaction():
                        await conn.execute(sql, case["case_id"])
        # A title that is no longer the placeholder stays locked even with the setting.
        await legacy_cases.apply(conn)
        async with conn.transaction():
            await conn.execute("SELECT set_config('booking.legacy_display_fix', 'aa1088', true)")
            with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match=LOCK_ERROR):
                await conn.execute(
                    f"UPDATE booking.trip_case_versions SET title = 'Other' WHERE id = ({version})",  # noqa: S608
                    case["case_id"],
                )
    finally:
        await conn.close()
