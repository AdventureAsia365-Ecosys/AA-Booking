"""bk-058 (AA-1096, S280) on a real PostgreSQL: booking.policy_row_closures ends a pending pricing rule, FX rate or
cancellation policy (rejected by a second person with a reason, withdrawn by its maker). Append-only for role
``booking``; a closure needs the row's real maker and a pending row; a closed row is never approved."""

from __future__ import annotations

import asyncpg
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MAKER, CHECKER = "it-1096-maker", "it-1096-checker"


async def _staff(conn: asyncpg.Connection) -> None:
    for staff_id in (MAKER, CHECKER):
        await conn.execute(
            "INSERT INTO booking.staff_users (cis_user_id, role, active) VALUES ($1, 'contracting', true)"
            " ON CONFLICT (cis_user_id) DO NOTHING",
            staff_id,
        )


async def _fx(conn: asyncpg.Connection, currency: str) -> str:
    return await conn.fetchval(
        "INSERT INTO booking.fx_rates (currency, rate_per_usd, buffer_pct, as_of, source, created_by)"
        " VALUES ($1, 1.5, 0, DATE '2031-01-01', 'it-1096', $2) RETURNING id::text",
        currency,
        MAKER,
    )


CLOSE = (
    "INSERT INTO booking.policy_row_closures (entity_type, fx_rate_id, event, maker, actor, reason)"
    " VALUES ('fx_rate', $1::uuid, $2, $3, $4, $5)"
)
APPROVE = "INSERT INTO booking.policy_approvals (entity_type, fx_rate_id, approved_by) VALUES ('fx_rate', $1::uuid, $2)"


async def test_closures_follow_maker_checker_and_block_approval(
    migrated_admin_dsn: str, migrated_booking_dsn: str
) -> None:
    admin = await asyncpg.connect(migrated_admin_dsn)
    try:
        await _staff(admin)
    finally:
        await admin.close()
    conn = await asyncpg.connect(migrated_booking_dsn)
    tx = conn.transaction()
    await tx.start()
    try:
        rejected, withdrawn, approved = await _fx(conn, "XAA"), await _fx(conn, "XAB"), await _fx(conn, "XAC")
        for args in (
            (rejected, "rejected", MAKER, MAKER, "x"),  # the maker never rejects
            (rejected, "rejected", MAKER, CHECKER, None),  # a rejection needs a reason
            (withdrawn, "withdrawn", MAKER, CHECKER, None),  # only the maker withdraws
            (rejected, "rejected", CHECKER, MAKER, "x"),  # the maker is the row's creator (trigger)
        ):
            with pytest.raises(asyncpg.CheckViolationError):
                async with conn.transaction():
                    await conn.execute(CLOSE, *args)
        await conn.execute(CLOSE, rejected, "rejected", MAKER, CHECKER, "Wrong rate")
        await conn.execute(CLOSE, withdrawn, "withdrawn", MAKER, MAKER, None)
        with pytest.raises(asyncpg.UniqueViolationError):  # one closure per row
            async with conn.transaction():
                await conn.execute(CLOSE, withdrawn, "withdrawn", MAKER, MAKER, None)
        with pytest.raises(asyncpg.CheckViolationError):  # a closed row is never approved
            async with conn.transaction():
                await conn.execute(APPROVE, rejected, CHECKER)
        await conn.execute(APPROVE, approved, CHECKER)
        with pytest.raises(asyncpg.CheckViolationError):  # an approved row is never closed
            async with conn.transaction():
                await conn.execute(CLOSE, approved, "withdrawn", MAKER, MAKER, None)
        with pytest.raises(asyncpg.InsufficientPrivilegeError):  # append-only for role booking
            async with conn.transaction():
                await conn.execute("UPDATE booking.policy_row_closures SET reason = 'edited'")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with conn.transaction():
                await conn.execute("DELETE FROM booking.policy_row_closures")
    finally:
        await tx.rollback()
        await conn.close()
