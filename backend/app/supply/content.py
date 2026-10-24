"""The customer content CIS publishes for a hotel or a service, read back live (AA-835 S4, root ADR 0004a decision 4).

CIS drafts a description, highlights and photos from the supply facts (``booking.v_supply_*``), a human reviews and
publishes them in CIS, and CIS serves them back through the contract view ``catalog.v_supply_content`` (CIS 278,
granted to role ``booking`` under the ``catalog.*`` contract grant). Booking never writes or copies that content
(ADR 0002): the hotel / service pages show it read-only with an "Edit in CIS" link, and proposal items carry only the
**published** description and highlights.

**Off until CIS 278 is applied** (feature detection, the same mechanism as ``app.catalog.liveness``): the view must
exist with the columns read here and role ``booking`` must be allowed to read it (checked once a minute per process,
catalog lookups only). Until then, or when a read fails (missing grant, timeout, or any other database error, such as
a column type CIS changed), ``content`` is ``None`` and the staff UI hides the panel: the switch turns on by itself
when CIS ships the view, with no Booking deploy or env change. Feature detection checks column names, not types, so
the SQL compares and returns the key columns as text, and every read runs in a savepoint that catches any
``asyncpg.PostgresError``: a CIS migration alone can never fail a Booking read or a proposal write.

What a row says (CIS owns the meaning): ``content_status`` ``draft | in_review | published | stale | unpublished``
(``none`` here when CIS holds no row for the entity yet); the text and ``photo_urls`` are non-null only while
published; ``edit_url`` is the CIS admin path of the entity (``/admin/supply-content/<id>``). The link is built from
that path only, never from Booking data, and a path of any other shape is dropped. Photo URLs must be ``https://``.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import asyncpg

from app.catalog.liveness import View, view_ready

log = logging.getLogger(__name__)

SOURCE_SYSTEM = "booking"
CONTENT_VIEW = View(
    "catalog",
    "v_supply_content",
    (
        "source_system",
        "source_id",
        "entity_type",
        "content_status",
        "content_version",
        "description",
        "highlights",
        "photo_urls",
        "edit_url",
    ),
)
NO_CONTENT = "none"
PUBLISHED = "published"
EDIT_PATH = re.compile(r"/admin/supply-content/[A-Za-z0-9_-]{1,64}")
MAX_HIGHLIGHTS = 20
MAX_PHOTOS = 40
# Wider than app.catalog.destinations.CATALOG_ERRORS on purpose: CIS owns this view and may change a column's type
# (uuid source_id, text content_version, ...), which raises e.g. UndefinedFunctionError. The read is optional, so any
# database error turns it off for that request instead of failing it.
READ_ERRORS: tuple[type[Exception], ...] = (asyncpg.PostgresError, TimeoutError)

# to_jsonb(...)::text: the array columns read the same whether CIS declares them text[] or jsonb. The ::text casts
# keep the keys comparable whatever type CIS gives them (text or uuid ids, text or enum statuses).
CONTENT_SQL = """
    SELECT c.entity_type::text AS entity_type, c.source_id::text AS source_id, c.content_status::text AS content_status,
           c.content_version, c.description::text AS description, to_jsonb(c.highlights)::text AS highlights,
           to_jsonb(c.photo_urls)::text AS photo_urls, c.edit_url::text AS edit_url
    FROM catalog.v_supply_content c
    WHERE c.source_system::text = 'booking'
"""


def _texts(raw: str | None, *, limit: int, https_only: bool = False) -> list[str]:
    try:
        values = json.loads(raw) if raw else []
    except ValueError:
        return []
    if not isinstance(values, list):
        return []
    out = [v.strip() for v in values if isinstance(v, str) and v.strip()]
    if https_only:
        out = [v for v in out if v.startswith("https://")]
    return out[:limit]


def _edit_path(raw: Any) -> str | None:
    return raw if isinstance(raw, str) and EDIT_PATH.fullmatch(raw) else None


def _content(row: Any) -> dict[str, Any]:
    published = row["content_status"] == PUBLISHED
    return {
        "content_status": row["content_status"],
        "content_version": row["content_version"],
        "description": row["description"] if published else None,
        "highlights": _texts(row["highlights"], limit=MAX_HIGHLIGHTS) if published else [],
        "photo_urls": _texts(row["photo_urls"], limit=MAX_PHOTOS, https_only=True) if published else [],
        "edit_url": _edit_path(row["edit_url"]),
    }


EMPTY = {
    "content_status": NO_CONTENT,
    "content_version": None,
    "description": None,
    "highlights": [],
    "photo_urls": [],
    "edit_url": None,
}


async def read_content(conn: asyncpg.Connection, entity_type: str, entity_id: str) -> dict[str, Any] | None:
    """The CIS content of one hotel / service; ``None`` while the read-back is off or the view cannot be read."""
    if not await view_ready(conn, CONTENT_VIEW):
        return None
    try:
        async with conn.transaction():  # a savepoint inside a caller's transaction
            row = await conn.fetchrow(
                f"{CONTENT_SQL} AND c.source_id::text = $1 AND c.entity_type::text = $2",  # noqa: S608 — constant
                entity_id,
                entity_type,
            )
    except READ_ERRORS as exc:
        log.warning("catalog.v_supply_content unreadable, content omitted: %s", type(exc).__name__)
        return None
    return dict(EMPTY) if row is None else _content(row)


async def published_texts(
    conn: asyncpg.Connection, refs: set[tuple[str, str]]
) -> dict[tuple[str, str], dict[str, Any]]:
    """``{(entity_type, entity_id): {description, highlights}}`` of the **published** entities among ``refs``;
    ``{}`` while the read-back is off or the view cannot be read (fail soft)."""
    if not refs or not await view_ready(conn, CONTENT_VIEW):
        return {}
    try:
        async with conn.transaction():
            rows = await conn.fetch(
                f"{CONTENT_SQL} AND c.source_id::text = ANY($1::text[])"  # noqa: S608 — constant
                " AND c.content_status::text = 'published'",
                sorted({entity_id for _, entity_id in refs}),
            )
    except READ_ERRORS as exc:
        log.warning("catalog.v_supply_content unreadable, proposal texts omitted: %s", type(exc).__name__)
        return {}
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["entity_type"], row["source_id"])
        if key in refs and row["description"]:
            out[key] = {
                "description": row["description"],
                "highlights": _texts(row["highlights"], limit=MAX_HIGHLIGHTS),
            }
    return out


async def attach_to_items(conn: asyncpg.Connection, items: list[dict[str, Any]]) -> None:
    """Give each version item that books a room type or a service the published description and highlights of its
    hotel / service (``supply.description`` / ``supply.highlights``; null / [] when CIS has not published any)."""
    booked = [item["supply"] for item in items if item.get("supply")]
    if not booked:
        return
    texts = await published_texts(
        conn, {(s["entity_type"], s["entity_id"]) for s in booked if s.get("entity_type") and s.get("entity_id")}
    )
    for supply in booked:
        text = texts.get((supply.get("entity_type"), supply.get("entity_id")), {})
        supply["description"] = text.get("description")
        supply["highlights"] = text.get("highlights", [])
