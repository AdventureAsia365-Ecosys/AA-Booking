"""B2B refusal of tours that are not served (AA-986 slice L5; root ADR 0015 decision 5, Q4).

``POST /quotes``, ``POST /holds`` and ``POST /holds/{id}/confirm`` refuse a ``tp:`` line whose trip plan's catalog key
is not served (:mod:`app.catalog.liveness`: not in ``catalog.v_tour``, or ``catalog.v_tour_retired`` says retired /
paused) with 409 ``tour_not_served`` naming the line, the tour, ``tour_state`` and ``reason``. Only new sales are
refused: a replay of an existing hold or of a confirmed booking still answers, and confirmed bookings are kept (staff
see a flag and get a notification, :mod:`app.catalog.lifecycle`). Cancelling is a staff decision, never automatic.

Only ``tp:`` lines carry a tour: ``rt:`` (a hotel room type) and ``sv:`` (a service) are supply items that exist
without a tour and stay bookable on their own. A ``departure_id`` belongs to the ``tp:`` line of its own plan only, so
it is covered by that line.
"""

from __future__ import annotations

from collections.abc import Iterable

import asyncpg

from app.b2b.components import PREFIX_PLAN, parse_component
from app.b2b.errors import B2BError
from app.catalog import liveness
from app.catalog.destinations import CATALOG_ERRORS

PLAN_KEYS_SQL = (
    "SELECT id::text AS id, source_id FROM booking.trip_plans WHERE id = ANY($1::uuid[]) AND source_system = 'cis'"
)
WORDS = {
    liveness.RETIRED: "is no longer offered by AA",
    liveness.PAUSED: "is temporarily unavailable",
    liveness.NOT_IN_CATALOG: "is not in the AA catalog",
}


def refusal(line_ref: str, state: liveness.KeyState) -> B2BError:
    reason = state.reason or state.state
    return B2BError(
        "tour_not_served",
        f"line {line_ref}: tour {state.source_id} {WORDS.get(state.state, 'is not served')} ({reason});"
        " remove the line or offer another tour",
        tour={"source_system": liveness.SOURCE_SYSTEM, "source_id": state.source_id},
        tour_state=state.state,
        reason=reason,
    )


async def require_served(conn: asyncpg.Connection, lines: Iterable[tuple[str, str]]) -> None:
    """``lines`` = ``(line_ref, component_id)``. Raises 409 ``tour_not_served`` for the first ``tp:`` line (request
    order) whose plan's tour is not served; 503 ``unavailable`` when the catalog cannot be read (fail closed). An
    unknown plan id is left to the pricing check (422 ``invalid``)."""
    plan_lines: list[tuple[str, str]] = []
    for line_ref, component_id in lines:
        try:
            prefix, ident = parse_component(component_id)
        except ValueError:
            continue  # the pricing check names it
        if prefix == PREFIX_PLAN:
            plan_lines.append((line_ref, ident))
    if not plan_lines:
        return
    keys = {r["id"]: r["source_id"] for r in await conn.fetch(PLAN_KEYS_SQL, sorted({p for _, p in plan_lines}))}
    if not keys:
        return
    try:
        found = await liveness.states(conn, keys.values(), strict=True)
    except CATALOG_ERRORS:
        raise B2BError("unavailable", "the catalog is not readable right now", retry_after_s=60) from None
    for line_ref, plan_id in plan_lines:
        source_id = keys.get(plan_id)
        if source_id is None:
            continue
        state = found.get(source_id) or liveness.KeyState(source_id, liveness.NOT_IN_CATALOG, liveness.NOT_IN_CATALOG)
        if not state.served:
            raise refusal(line_ref, state)
