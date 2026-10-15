"""Catalog tour key of trip case legs (AA-830, data standards X3 / DM-2.2; migration bk-011).

A leg's ``source_tour_id`` is whatever TripPlanner keyed the tour by: a catalog ``source_id`` for some tours, a
version ``tour_id`` (changes on every CIS re-upload) for others. The stable key is ``(source_system, source_id)``
of contract ``catalog.tour.v1``. This module maps ``source_tour_id`` values to that key through
``catalog.v_tour`` (live tours only; both key kinds accepted) and back-fills legs that have none.

``resolve``
    Used by ``bk_inquiry_process`` for legs that arrive without a key (v1 / v1.1 payloads, or a v1.2 leg
    TripPlanner could not resolve). Soft: an unreadable catalog leaves the key null, never fails the intake.

``backfill`` / ``python -m app.inquiry.leg_keys [--apply]``
    One-off, re-runnable back-fill of ``booking.trip_case_legs`` rows whose key is null. DRY RUN by default: it
    only reports what it would write and every leg it cannot match. ``--apply`` writes the matched keys (only rows
    still null, so a re-run never overwrites). It also reports the TripPlanner outbox legs
    (``tripplanner.v_inquiry_outbox``, read-only: published events are never rewritten). Runs in the Booking task
    (role ``booking``: UPDATE on the table since bk-002, SELECT on ``catalog.v_tour`` (CIS 225) and the outbox view
    (tp-009)) with ``DATABASE_URL`` from the task environment; prints one JSON report.

A tour_id of a superseded tour version is not in ``catalog.v_tour`` (live tours only) and Booking may not read
the CIS pipeline tables (ADR 0002), so such a leg stays unmatched and is listed for a person to resolve.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any

import asyncpg

from app.catalog import CATALOG_ERRORS

log = logging.getLogger(__name__)

TourKey = tuple[str, str]  # (source_system, source_id)

# Both key kinds in one read. catalog.v_tour holds ~900 live tours; the text comparison on tour_id is a filter over
# them (measured 8 ms on Dev, S238). A source_id match wins over a tour_id match (they never collide on Dev).
TOUR_KEYS_SQL = (
    "SELECT source_system, source_id, tour_id::text AS tour_id FROM catalog.v_tour"
    " WHERE source_id = ANY($1::text[]) OR tour_id::text = ANY($1::text[])"
)

MISSING_LEGS_SQL = """
    SELECT l.id::text AS id, tc.code, l.position, l.source_tour_id
    FROM booking.trip_case_legs l JOIN booking.trip_cases tc ON tc.id = l.trip_case_id
    WHERE l.source_id IS NULL
    ORDER BY tc.code, l.position
"""

FILL_SQL = (
    "UPDATE booking.trip_case_legs SET source_system = $2, source_id = $3 WHERE id = $1::uuid AND source_id IS NULL"
)

OUTBOX_LEGS_SQL = """
    SELECT o.idempotency_key, (leg ->> 'position')::int AS position, leg ->> 'source_tour_id' AS source_tour_id,
           leg ->> 'source_id' AS source_id, o.payload ->> 'version' AS version
    FROM tripplanner.v_inquiry_outbox o
    CROSS JOIN LATERAL jsonb_array_elements(coalesce(o.payload -> 'itinerary' -> 'legs', '[]'::jsonb)) AS leg
    ORDER BY o.id, position
"""


def _match(rows: list[Any], wanted: list[str]) -> dict[str, TourKey]:
    by_source: dict[str, TourKey] = {}
    by_tour: dict[str, TourKey] = {}
    for r in rows:
        key = (r["source_system"], r["source_id"])
        by_source[r["source_id"]] = key
        by_tour[r["tour_id"]] = key
    return {k: by_source.get(k) or by_tour[k] for k in wanted if k in by_source or k in by_tour}


async def resolve(conn: asyncpg.Connection, keys: list[str | None]) -> dict[str, TourKey]:
    """Map each ``source_tour_id`` (either key kind) to its catalog key; keys not in the catalog are absent.

    Read in a savepoint: an unreadable catalog (225 not applied, grant missing) returns {} and leaves the caller's
    transaction usable."""
    wanted = sorted({k for k in keys if k})
    if not wanted:
        return {}
    try:
        async with conn.transaction():
            rows = await conn.fetch(TOUR_KEYS_SQL, wanted)
    except CATALOG_ERRORS as exc:
        log.warning("catalog.v_tour unreadable, legs keep a null tour key: %s", type(exc).__name__)
        return {}
    return _match(list(rows), wanted)


async def backfill(conn: asyncpg.Connection, *, apply: bool = False) -> dict[str, Any]:
    """Fill (or with ``apply=False`` only report) the catalog key of legs that have none. Re-runnable."""
    legs = [dict(r) for r in await conn.fetch(MISSING_LEGS_SQL)]
    keys = await resolve(conn, [leg["source_tour_id"] for leg in legs])
    matched = [leg for leg in legs if leg["source_tour_id"] in keys]
    unmatched = [leg for leg in legs if leg["source_tour_id"] not in keys]
    written = 0
    if apply and matched:
        async with conn.transaction():
            for leg in matched:
                system, source_id = keys[leg["source_tour_id"]]
                status = await conn.execute(FILL_SQL, leg["id"], system, source_id)
                written += int(str(status).rsplit(" ", 1)[-1] or 0)
    return {
        "mode": "apply" if apply else "dry_run",
        "legs_without_key": len(legs),
        "matched": len(matched),
        "written": written,
        "unmatched": [
            {"trip_case": leg["code"], "position": leg["position"], "source_tour_id": leg["source_tour_id"]}
            for leg in unmatched
        ],
        "would_write": []
        if apply
        else [
            {
                "trip_case": leg["code"],
                "position": leg["position"],
                "source_tour_id": leg["source_tour_id"],
                "source_system": keys[leg["source_tour_id"]][0],
                "source_id": keys[leg["source_tour_id"]][1],
            }
            for leg in matched
        ],
    }


async def outbox_report(conn: asyncpg.Connection) -> dict[str, Any]:
    """TripPlanner outbox legs and whether their key is in the catalog (read-only; events are never rewritten)."""
    try:
        legs = [dict(r) for r in await conn.fetch(OUTBOX_LEGS_SQL)]
    except (asyncpg.UndefinedTableError, asyncpg.InsufficientPrivilegeError) as exc:
        return {"error": type(exc).__name__}
    keys = await resolve(conn, [leg["source_id"] or leg["source_tour_id"] for leg in legs])
    unmatched = [leg for leg in legs if (leg["source_id"] or leg["source_tour_id"]) not in keys]
    return {
        "legs": len(legs),
        "with_key_in_payload": sum(1 for leg in legs if leg["source_id"]),
        "matched": len(legs) - len(unmatched),
        "unmatched": unmatched,
    }


async def _main(apply: bool) -> int:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print(json.dumps({"error": "DATABASE_URL not set"}))
        return 2
    conn = await asyncpg.connect(
        dsn,
        ssl=os.environ.get("DB_SSL", "require"),
        timeout=10,
        server_settings={"application_name": "booking-leg-keys"},
    )
    try:
        report = {"trip_case_legs": await backfill(conn, apply=apply), "tripplanner_outbox": await outbox_report(conn)}
    finally:
        await conn.close()
    print(json.dumps(report, indent=1, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Back-fill the catalog tour key of booking.trip_case_legs (AA-830).")
    parser.add_argument("--apply", action="store_true", help="write the matched keys (default: DRY RUN, report only)")
    args = parser.parse_args(argv)
    return asyncio.run(_main(args.apply))


if __name__ == "__main__":
    sys.exit(main())
