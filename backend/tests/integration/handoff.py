"""TripPlanner hand-off rows for the integration tests (AA-1046: the Booking intake that read them is retired).

The ``tripplanner.outbox`` rows are still read by ``app.inquiry.leg_keys.outbox_report`` (the AA-830 back-fill report),
and the kept b2c trip cases still carry the legs the intake wrote. :func:`publish` is what TripPlanner ``handoff.py``
does; :func:`case_with_legs` writes a kept b2c case with its legs the way the intake stored them.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from tests.integration import ops

# What the leg tests clear before each test (CASCADE takes the rows that point at the cases).
LEG_TABLES = (
    "booking.notifications, booking.trip_case_legs, booking.trip_cases, booking.inbox_cursor, "
    "booking.inbox_events, booking.job, booking.audit_logs"
)


async def publish(conn: asyncpg.Connection, payload: dict[str, Any], *, age_seconds: int = 0) -> int:
    """One outbox row per hand-off, as TripPlanner writes it."""
    return await conn.fetchval(
        """
        INSERT INTO tripplanner.outbox (id, event, schema_version, idempotency_key, aggregate_id, payload, created_at)
        VALUES (nextval('tripplanner.outbox_id_seq'), 'inquiry.submitted', 1, $1, $2::uuid, $3::jsonb,
                now() - make_interval(secs => $4))
        RETURNING id
        """,
        payload["idempotency_key"],
        payload["trip_id"],
        json.dumps(payload),
        age_seconds,
    )


async def case_with_legs(
    conn: asyncpg.Connection, payload: dict[str, Any], *, keys: list[str | None] | None = None
) -> str:
    """A kept b2c case (source ``tripplanner``) with one leg per payload leg; ``keys[i]`` is the catalog key the leg
    came with (``cis`` + source_id), None for a leg without one (a pre-bk-011 leg looks the same)."""
    case = await ops.insert_b2c_case(
        conn, tag=f"legs-{payload['trip_id'][:8]}", status="new_inquiry", start=None, end=None,
        source_system="tripplanner",
    )  # fmt: skip
    legs = sorted(payload["itinerary"]["legs"], key=lambda leg: leg["position"])
    for i, leg in enumerate(legs):
        key = keys[i] if keys else None
        await conn.execute(
            "INSERT INTO booking.trip_case_legs (trip_case_id, position, source_tour_id, day_from, day_to,"
            " source_system, source_id) VALUES ($1::uuid, $2, $3, $4, $5, $6, $7)",
            case["id"],
            leg["position"],
            leg["source_tour_id"],
            leg["day_from"],
            leg["day_to"],
            "cis" if key else None,
            key,
        )
    return case["id"]


async def inbox_row(
    conn: asyncpg.Connection,
    payload: Any,
    *,
    status: str = "processed",
    error: str | None = None,
    key: str | None = None,
    version: str | None = None,
) -> str:
    """One ``inquiry.submitted`` inbox row as the retired intake left it: ``processed`` (a case was opened),
    ``parked`` (no usable contact, with its fixed reason) or ``failed`` (contract error); ``received`` = never
    processed. The kept rows are what the DM-6.6 redaction (``bk_retention_inbox_redact``) works on."""
    if key is None:
        key = payload["idempotency_key"]
    return await conn.fetchval(
        "INSERT INTO booking.inbox_events (source, contract, idempotency_key, payload, status, error, processed_at,"
        " contract_version) VALUES ('tripplanner', 'inquiry.submitted', $1, $2::jsonb, $3, $4,"
        " CASE WHEN $3 = 'received' THEN NULL ELSE now() END, $5) RETURNING id::text",
        key,
        json.dumps(payload),
        status,
        error,
        version if version is not None else (str(payload.get("version")) if isinstance(payload, dict) else None),
    )
