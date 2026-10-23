"""Is a CIS catalog key served? Booking's read of the tour served state (AA-986 slice L5, root ADR 0015 decision 5).

Booking reads CIS only through the ``catalog`` contract views, as role ``booking``:

- ``catalog.v_tour`` (CIS 225): the live tours. A key in it is served.
- ``catalog.v_tour_retired`` (CIS 274, contract catalog.tour.v1.3): one row per key whose latest lifecycle event is
  retired, paused or superseded, with ``state`` (``retired`` | ``paused``), ``reason`` and ``changed_at``.
- ``catalog.v_tour_lifecycle_outbox`` (CIS 274): the append-only events, pulled by ``bk_tour_lifecycle_pull``
  (:mod:`app.catalog.lifecycle`).

**Rule** (ADR 0015 decision 5, AA-Booking): a key is *not served* when it is not in ``catalog.v_tour`` **or** when
``v_tour_retired`` calls it ``retired`` / ``paused``. Fail closed: Booking never sells a key that either view calls
gone; a disagreement is logged (``catalog_liveness_disagree``) and is what the nightly probe of ADR 0015 decision 6
reports. States: ``served``, ``retired``, ``paused`` (temporary: Master inactive or a supersede waiting for its new
Master) and ``not_in_catalog`` (absent from ``v_tour`` with no tombstone: never served, or CIS 274 not applied yet).

**Degrades safely without CIS 274** (feature detection, not a startup failure): :func:`view_ready` checks once a
minute per process that the view exists, that role ``booking`` may read it and that it has the columns this module
selects (catalog lookups only, so it can never raise or abort the caller's transaction). Until then the read uses
``catalog.v_tour`` alone: a missing key is ``not_in_catalog`` (still refused for sale), and the reason is unknown.

Two read modes: ``strict`` (B2B quote / hold / confirm: a catalog error propagates, the caller answers 503) and soft
(staff screens: a catalog error inside a savepoint gives "no state", never a failed page or a failed write).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import asyncpg

from app.catalog.destinations import CATALOG_ERRORS

log = logging.getLogger(__name__)

SOURCE_SYSTEM = "cis"

SERVED = "served"
RETIRED = "retired"
PAUSED = "paused"
NOT_IN_CATALOG = "not_in_catalog"
NOT_SERVED = frozenset({RETIRED, PAUSED, NOT_IN_CATALOG})


@dataclass(frozen=True)
class View:
    schema: str
    name: str
    columns: tuple[str, ...]

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}"


# The columns Booking selects (ADR 0015 decision 4 names; the CIS 274 PR fixes them). A view without one of them is
# treated as missing, so a renamed column degrades the feature instead of failing a request.
RETIRED_VIEW = View("catalog", "v_tour_retired", ("source_system", "source_id", "state", "reason", "changed_at"))
OUTBOX_VIEW = View(
    "catalog",
    "v_tour_lifecycle_outbox",
    ("id", "idempotency_key", "source_system", "source_id", "event", "reason", "occurred_at"),
)

CACHE_SECONDS = 60.0
# Catalog lookups only (no relation is read): never raises, so it is safe inside any transaction.
VIEW_READY_SQL = """
SELECT has_schema_privilege(n.oid, 'USAGE') AND has_table_privilege(c.oid, 'SELECT')
       AND (SELECT count(*) FROM pg_attribute a
             WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped AND a.attname = ANY($3::text[]))
           = cardinality($3::text[])
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = $1 AND c.relname = $2
"""

_ready: dict[str, tuple[bool, float]] = {}


def reset_cache() -> None:
    """Forget the feature detection (tests; a catalog error on a view that was ready)."""
    _ready.clear()


async def view_ready(conn: asyncpg.Connection, view: View) -> bool:
    """True when ``view`` exists with its columns and role ``booking`` may read it (cached ``CACHE_SECONDS``)."""
    now = time.monotonic()
    cached = _ready.get(view.qualified)
    if cached is not None and cached[1] > now:
        return cached[0]
    ready = bool(await conn.fetchval(VIEW_READY_SQL, view.schema, view.name, list(view.columns)))
    if cached is None or cached[0] != ready:
        if ready:
            log.info("catalog_view_ready view=%s", view.qualified)
        else:
            log.warning("catalog_view_unready view=%s (CIS 274 not applied, or no grant): degraded", view.qualified)
    _ready[view.qualified] = (ready, now + CACHE_SECONDS)
    return ready


@dataclass(frozen=True)
class KeyState:
    """The served state of one catalog key as Booking sees it."""

    source_id: str
    state: str
    reason: str | None = None
    changed_at: datetime | None = None

    @property
    def served(self) -> bool:
        return self.state == SERVED

    def out(self) -> dict[str, Any]:
        return {
            "source_system": SOURCE_SYSTEM,
            "source_id": self.source_id,
            "state": self.state,
            "reason": self.reason,
            "changed_at": self.changed_at,
        }


def states_sql(with_retired: bool) -> str:
    """One row per asked key (``unnest``): in ``v_tour`` (correlated equality, pushed into the view per key, never an
    ``= ANY`` filter through its joins) and, with CIS 274, the newest tombstone row of the key."""
    if not with_retired:
        return """
SELECT k.source_id,
       EXISTS (SELECT 1 FROM catalog.v_tour t WHERE t.source_system = 'cis' AND t.source_id = k.source_id)
           AS in_catalog,
       NULL::text AS tomb_state, NULL::text AS tomb_reason, NULL::timestamptz AS tomb_changed_at
FROM unnest($1::text[]) AS k(source_id)
"""
    return """
SELECT k.source_id,
       EXISTS (SELECT 1 FROM catalog.v_tour t WHERE t.source_system = 'cis' AND t.source_id = k.source_id)
           AS in_catalog,
       r.state AS tomb_state, r.reason AS tomb_reason, r.changed_at AS tomb_changed_at
FROM unnest($1::text[]) AS k(source_id)
LEFT JOIN LATERAL (
    SELECT r.state, r.reason, r.changed_at FROM catalog.v_tour_retired r
    WHERE r.source_system = 'cis' AND r.source_id = k.source_id
    ORDER BY r.changed_at DESC NULLS LAST
    LIMIT 1
) r ON true
"""


def key_state(row: Any) -> KeyState:
    """The rule of the module docstring over one row of :func:`states_sql`."""
    source_id = row["source_id"]
    tomb = row["tomb_state"]
    if tomb is not None and tomb != SERVED:
        state = RETIRED if tomb == RETIRED else PAUSED  # any other tombstone state is temporary (ADR 0015 Q11)
        if row["in_catalog"]:
            log.warning("catalog_liveness_disagree source_id=%s tombstone=%s in_v_tour=true", source_id, tomb)
        return KeyState(source_id, state, row["tomb_reason"], row["tomb_changed_at"])
    if row["in_catalog"]:
        return KeyState(source_id, SERVED)
    return KeyState(source_id, NOT_IN_CATALOG, NOT_IN_CATALOG)


async def _read(conn: asyncpg.Connection, keys: list[str]) -> dict[str, KeyState]:
    with_retired = await view_ready(conn, RETIRED_VIEW)
    try:
        rows = await conn.fetch(states_sql(with_retired), keys)
    except CATALOG_ERRORS:
        if with_retired:
            reset_cache()  # the view went away (or its grant) since it was detected: detect again next time
        raise
    return {r["source_id"]: key_state(r) for r in rows}


async def states(conn: asyncpg.Connection, keys: Iterable[str | None], *, strict: bool = False) -> dict[str, KeyState]:
    """``{source_id: KeyState}`` for the distinct non-empty keys.

    ``strict``: a catalog error (view or grant missing) propagates; the caller decides (B2B answers 503).
    Otherwise the read runs in a savepoint and a catalog error gives ``{}`` (the screen shows no flag), so a staff
    read or a write that shows the case afterwards never fails on the catalog."""
    wanted = sorted({k for k in keys if k})
    if not wanted:
        return {}
    if strict:
        return await _read(conn, wanted)
    try:
        async with conn.transaction():
            return await _read(conn, wanted)
    except CATALOG_ERRORS as exc:
        log.warning("catalog_liveness_unreadable error=%s (no served-state flags)", type(exc).__name__)
        return {}
