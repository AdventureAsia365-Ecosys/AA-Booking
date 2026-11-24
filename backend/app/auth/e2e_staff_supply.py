"""Synthetic-supply checks of the Dev-only E2E writers, beside :mod:`app.auth.e2e_staff` (A10e-4 / A10e-5, S289).

:mod:`app.auth.e2e_staff` holds the writer identities, their route allow-list and the synthetic-path SQL; this module
holds the checks kept out of that file so it stays within the code-health size cap. Same logger, same refusal codes:

- A10e-4 (AA-1151): the per-row check of the activity catalogue import
  (:data:`app.auth.e2e_staff.ACTIVITY_SHEET_IMPORT`).
- A10e-5 (AA-1153): the record checks of the writes in :data:`app.auth.e2e_staff.RECORD_CHECKED` (:func:`check_record`):
  a hotel to DMC link needs the hotel (path) and the DMC (body) both synthetic supply of the E2E pricing maker; a desk
  entry needs the agency synthetic in CIS (``catalog.v_tenant_account_v1.is_synthetic``) and every room type of its
  lines on a synthetic hotel whose every DMC link (any status) is to a synthetic supplier, so no real DMC's rate prices
  it and no real DMC is booked. Fail closed: an unknown or malformed id, a missing row or a database error answer False.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import asyncpg
from fastapi import HTTPException

from app.auth import e2e_staff_bodies as bodies
from app.auth.e2e_staff import (
    DESK_ENTRY,
    E2E_PRICING_MAKER_EMAIL,
    E2E_READ_ONLY,
    HOTEL_SUPPLIER_LINK,
    HOTEL_SYNTHETIC_SQL,
    SUPPLIER_SYNTHETIC_SQL,
    SYNTHETIC_SUPPLY_MARK,
    _lookup,
    _uuid,
    log,
)

# Each imported row queues a paid CIS draft job, so an E2E session imports at most this many.
ACTIVITY_SHEET_MAX_ROWS = 5  # technical-constant: the Dev-only E2E guard (root ADR 0006a A10e-4), not a policy


def check_activity_sheet(writer_name: str, sheet: Any) -> None:
    """A10e-4: the activity import route's check for an E2E session, before any preview or write (the path's supplier
    is synthetic). 403 ``e2e_read_only`` past :data:`ACTIVITY_SHEET_MAX_ROWS` rows or unless every row's title starts
    with :data:`~app.auth.e2e_staff.SYNTHETIC_SUPPLY_MARK` and has no inclusions.
    ``sheet`` = ``activity_import.parse(upload)``."""
    for i, row in enumerate(getattr(sheet, "rows", None) or ()):
        title = getattr(row, "title", None)
        in_cap = i < ACTIVITY_SHEET_MAX_ROWS
        marked = in_cap and isinstance(title, str) and title.strip().startswith(SYNTHETIC_SUPPLY_MARK)
        if not marked or (getattr(row, "service", None) or {}).get("inclusions"):
            log.info("booking_e2e_writer_refused identity=%s reason=activity_row", writer_name)
            raise HTTPException(status_code=403, detail=E2E_READ_ONLY)


# A10e-5: a supplier (alias ``{a}``, its creator's staff row ``{u}``) is the E2E pricing maker's ($3) synthetic one.
_MAKERS = "coalesce(lower({u}.email) = $3 AND {a}.company_name LIKE '" + SYNTHETIC_SUPPLY_MARK + "%', false)"
_ROOM_HOTEL_OK = (
    _MAKERS.format(u="su", a="s") + " AND NOT EXISTS (SELECT 1 FROM booking.hotel_supplier_links l"  # noqa: S608 — constant fragments
    " JOIN booking.suppliers ls ON ls.id = l.supplier_id"
    " LEFT JOIN booking.staff_users lu ON lu.cis_user_id = ls.created_by"
    " WHERE l.hotel_id = r.id AND NOT " + _MAKERS.format(u="lu", a="ls") + ")"
)
# $1 the agency (b2b_partners.id), $2 the distinct room type ids, $3 the maker's address, $4 how many ids: True only
# when the agency is synthetic, every id is a known room type and each one's hotel passes _ROOM_HOTEL_OK.
DESK_ENTRY_SYNTHETIC_SQL = (
    "SELECT coalesce((SELECT t.is_synthetic IS TRUE FROM booking.b2b_partners p"  # noqa: S608 — constant fragments
    " JOIN catalog.v_tenant_account_v1 t ON t.tenant_id = p.tenant_id WHERE p.id = $1::uuid), false)"
    " AND (SELECT count(*) = $4::int AND coalesce(bool_and(coalesce(" + _ROOM_HOTEL_OK + ", false)), false)"
    " FROM booking.hotel_room_types rt JOIN booking.hotels r ON r.id = rt.hotel_id"
    " JOIN booking.suppliers s ON s.id = r.supplier_id"
    " LEFT JOIN booking.staff_users su ON su.cis_user_id = s.created_by"
    " WHERE rt.id = ANY($2::uuid[])) AS is_synthetic"
)


async def _fetch(conn: asyncpg.Connection | asyncpg.Pool, route: str, sql: str, *args: Any) -> bool:
    try:
        return (await conn.fetchval(sql, *args)) is True
    except (asyncpg.PostgresError, OSError) as exc:
        log.warning("booking_e2e_synthetic_unknown route=%s error_class=%s", route, type(exc).__name__)
        return False


async def _link_synthetic(conn: asyncpg.Connection | asyncpg.Pool, params: Mapping[str, Any], body: Any) -> bool:
    """The hotel of the path and the DMC of the body are both the E2E pricing maker's synthetic supply."""
    hotel = _lookup("hotel_ref", HOTEL_SYNTHETIC_SQL, params.get("hotel_ref"))
    dmc = _lookup("supplier_id", SUPPLIER_SYNTHETIC_SQL, body.get("supplier_id") if isinstance(body, dict) else None)
    if hotel is None or dmc is None:
        return False
    route = HOTEL_SUPPLIER_LINK[1]
    return await _fetch(conn, route, *hotel, E2E_PRICING_MAKER_EMAIL) and await _fetch(
        conn, route, *dmc, E2E_PRICING_MAKER_EMAIL
    )


async def _desk_entry_synthetic(conn: asyncpg.Connection | asyncpg.Pool, body: Any) -> bool:
    """A synthetic agency, and room lines on synthetic hotels only (:data:`DESK_ENTRY_SYNTHETIC_SQL`, one query).
    The agency and the room types by UUID only."""
    partner = _uuid(body.get("partner_id")) if isinstance(body, dict) else None
    rooms = [_uuid(r) for r in bodies.desk_room_ids(body)]
    if partner is None or not rooms or any(r is None for r in rooms):
        return False
    ids = sorted({r for r in rooms if r is not None})
    args = (partner, ids, E2E_PRICING_MAKER_EMAIL, len(ids))
    return await _fetch(conn, DESK_ENTRY[1], DESK_ENTRY_SYNTHETIC_SQL, *args)


async def check_record(
    conn: asyncpg.Connection | asyncpg.Pool, route: str, params: Mapping[str, Any], body: Any
) -> bool:
    """A10e-5: True only when the write of :data:`app.auth.e2e_staff.RECORD_CHECKED` touches synthetic records only."""
    if route == HOTEL_SUPPLIER_LINK[1]:
        return await _link_synthetic(conn, params, body)
    if route == DESK_ENTRY[1]:
        return await _desk_entry_synthetic(conn, body)
    return False
