"""AA-835 (bk-023) schema on a real PostgreSQL: registration and re-run no-op, owner bk_owner, grants (role booking:
supply columns of the outbox through the trigger only, photos insert + rights / status only and never deleted, views
read-only; cis_app: SELECT on the four v_supply_* views and nothing else), the contract views are security_barrier and
facts only (exact column lists, no supplier / rate / note / link / staff / audit column), and the CHECKs of the supply
stream and of booking.entity_media (derived key, bucket, type, size, sha256, status).

Behaviour (triggers, versions, views, readers) is in test_bk023_supply.py."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "023_supply_outbox_media.sql"
VIEWS = ("v_supply_hotels", "v_supply_services", "v_supply_media", "v_supply_outbox")
FUNCTIONS = (
    "hotels_supply_version",
    "services_supply_version",
    "supply_outbox",
    "hotel_room_types_supply_bump",
    "entity_media_guard",
    "entity_media_supply_bump",
)
BUCKET = "aa-booking-dev-supply-media-000000000000"
SHA = "a" * 64

# The contract field sets (AA-Ecosys docs/contracts/supply.hotel.v1.json / supply.service.v1.json) + id, supply_version.
VIEW_COLUMNS = {
    "v_supply_hotels": [
        ("id", "uuid"),
        ("supply_version", "bigint"),
        ("name", "text"),
        ("hotel_style", "text"),
        ("star_rating", "smallint"),
        ("address", "text"),
        ("destination_id", "uuid"),
        ("country_code", "text"),
        ("lat", "double precision"),
        ("lng", "double precision"),
        ("check_in_time", "time without time zone"),
        ("check_out_time", "time without time zone"),
        ("amenities", "ARRAY"),
        ("room_types", "jsonb"),
        ("status", "text"),
        ("media", "jsonb"),
    ],
    "v_supply_services": [
        ("id", "uuid"),
        ("supply_version", "bigint"),
        ("service_type", "text"),
        ("working_title", "text"),
        ("unit", "text"),
        ("destination_id", "uuid"),
        ("country_code", "text"),
        ("duration_minutes", "integer"),
        ("max_group_size", "smallint"),
        ("inclusions", "ARRAY"),
        ("status", "text"),
        ("media", "jsonb"),
    ],
    "v_supply_media": [
        ("media_id", "uuid"),
        ("entity_type", "text"),
        ("entity_id", "uuid"),
        ("usage_rights", "boolean"),
        ("s3_bucket", "text"),
        ("s3_key", "text"),
        ("sha256", "text"),
        ("content_type", "text"),
    ],
    "v_supply_outbox": [
        ("event_id", "uuid"),
        ("event_type", "text"),
        ("entity_type", "text"),
        ("entity_id", "uuid"),
        ("supply_version", "bigint"),
        ("occurred_at", "timestamp with time zone"),
        ("feed_seq", "bigint"),
    ],
}
# Never in a contract view (data standards DM-6.1 conf:commercial, staff ids, audit columns, free links).
FORBIDDEN_WORDS = ("supplier", "net", "rate", "margin", "note", "link", "url", "code_", "_by", "created", "updated")


class _Rollback(Exception):
    """Raised at the end of a test transaction so its rows are rolled back."""


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    try:
        # A replay of bk-022 (its tests re-run it) takes back the supply column grants on the outbox: replay bk-023.
        await conn.execute("SET ROLE bk_owner")
        await conn.execute(MIGRATION.read_text(encoding="utf-8"))
        await conn.execute("RESET ROLE")
        yield conn
    finally:
        await conn.close()


async def test_registered_and_rerun_is_a_no_op(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM booking.v_schema_versions WHERE version = 'bk-023'") == 1
    before = await admin.fetchval("SELECT count(*) FROM pg_catalog.pg_constraint")
    triggers = await admin.fetchval("SELECT count(*) FROM pg_catalog.pg_trigger WHERE NOT tgisinternal")
    indexes = await admin.fetchval("SELECT count(*) FROM pg_catalog.pg_index")
    await admin.execute("SET ROLE bk_owner")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    assert await admin.fetchval("SELECT count(*) FROM pg_catalog.pg_constraint") == before
    assert await admin.fetchval("SELECT count(*) FROM pg_catalog.pg_trigger WHERE NOT tgisinternal") == triggers
    assert await admin.fetchval("SELECT count(*) FROM pg_catalog.pg_index") == indexes


async def test_refuses_another_role(admin: asyncpg.Connection) -> None:
    """The opening guard: DB Migrate runs the file as bk_owner; the master itself is refused."""
    with pytest.raises(asyncpg.RaiseError, match="expected to run as bk_owner"):
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))


async def test_owner_and_functions(admin: asyncpg.Connection) -> None:
    owners = await admin.fetch(
        "SELECT relname, relowner::regrole::text AS owner, reloptions FROM pg_class"
        " WHERE relnamespace = 'booking'::regnamespace AND relname = ANY($1::text[])",
        ["entity_media", *VIEWS],
    )
    assert {r["relname"] for r in owners} == {"entity_media", *VIEWS}
    assert {r["owner"] for r in owners} == {"bk_owner"}
    for r in owners:
        if r["relname"] in VIEWS:
            assert "security_barrier=true" in (r["reloptions"] or []), r["relname"]
    procs = await admin.fetch(
        "SELECT proname, proowner::regrole::text AS owner, prosecdef FROM pg_proc"
        " WHERE pronamespace = 'booking'::regnamespace AND proname = ANY($1::text[])",
        list(FUNCTIONS),
    )
    assert {p["proname"] for p in procs} == set(FUNCTIONS)
    assert {p["owner"] for p in procs} == {"bk_owner"} and not any(p["prosecdef"] for p in procs)


async def test_grants_of_role_booking(admin: asyncpg.Connection) -> None:
    async def table(name: str, priv: str) -> bool:
        return await admin.fetchval("SELECT has_table_privilege('booking', $1, $2)", f"booking.{name}", priv)

    async def column(name: str, col: str, priv: str) -> bool:
        return await admin.fetchval("SELECT has_column_privilege('booking', $1, $2, $3)", f"booking.{name}", col, priv)

    # Outbox: the supply columns are insert-only (the trigger runs as the writer), never updated.
    for col in ("entity_type", "entity_id", "supply_version"):
        assert await column("outbox", col, "INSERT"), col
        assert not await column("outbox", col, "UPDATE"), col
    assert not await table("outbox", "INSERT") and not await table("outbox", "DELETE")
    # Photos: insert, then only the rights flag, the status and the audit columns; never deleted.
    assert await table("entity_media", "SELECT") and await table("entity_media", "INSERT")
    for priv in ("UPDATE", "DELETE", "TRUNCATE"):
        assert not await table("entity_media", priv), priv
    for col in ("usage_rights", "status", "updated_by", "updated_at"):
        assert await column("entity_media", col, "UPDATE"), col
    for col in ("entity_type", "entity_id", "s3_bucket", "s3_key", "sha256", "content_type", "bytes", "uploaded_by"):
        assert not await column("entity_media", col, "UPDATE"), col
    # Views: read-only for the app.
    for view in VIEWS:
        assert await table(view, "SELECT"), view
        for priv in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            assert not await table(view, priv), (view, priv)


async def test_cis_app_reads_the_four_views_only(admin: asyncpg.Connection) -> None:
    grants = await admin.fetch(
        "SELECT c.relname, a.privilege_type FROM pg_class c, aclexplode(c.relacl) a"
        " WHERE c.relnamespace = 'booking'::regnamespace AND a.grantee = 'cis_app'::regrole"
        " AND c.relname LIKE 'v\\_supply\\_%'"
    )
    assert sorted((r["relname"], r["privilege_type"]) for r in grants) == sorted((v, "SELECT") for v in VIEWS)
    for table in ("hotels", "hotel_room_types", "services", "entity_media", "outbox", "supplier_rates"):
        assert not await admin.fetchval("SELECT has_table_privilege('cis_app', $1, 'SELECT')", f"booking.{table}")


async def test_views_are_facts_only(admin: asyncpg.Connection) -> None:
    for view, expected in VIEW_COLUMNS.items():
        rows = await admin.fetch(
            "SELECT column_name, data_type FROM information_schema.columns"
            " WHERE table_schema = 'booking' AND table_name = $1 ORDER BY ordinal_position",
            view,
        )
        assert [tuple(r) for r in rows] == expected, view
        leaked = [name for name, _ in expected if any(w in name for w in FORBIDDEN_WORDS)]
        assert leaked == [], (view, leaked)
    # The JSON parts are built in the view text: they name only the contract keys.
    for view in ("v_supply_hotels", "v_supply_services"):
        text = await admin.fetchval("SELECT pg_get_viewdef($1::regclass)", f"booking.{view}")
        for word in ("internal_notes", "links", "supplier", "net_amount", "uploaded_by", "s3_key", "s3_bucket"):
            assert word not in text, (view, word)


async def test_supply_stream_checks(admin: asyncpg.Connection) -> None:
    """A supply row names its entity and version and nothing else; a b2b row never carries the supply columns."""
    ok = (
        "INSERT INTO booking.outbox (stream, event_type, entity_type, entity_id, supply_version)"
        " VALUES ('supply', 'supply.upsert', 'hotel', gen_random_uuid(), 1)"
    )
    bad = [
        # supply + a partner
        "INSERT INTO booking.outbox (stream, event_type, entity_type, entity_id, supply_version, partner_id)"
        " VALUES ('supply', 'supply.upsert', 'hotel', gen_random_uuid(), 1, gen_random_uuid())",
        # supply without a version / with version 0 / unknown entity type
        "INSERT INTO booking.outbox (stream, event_type, entity_type, entity_id)"
        " VALUES ('supply', 'supply.upsert', 'hotel', gen_random_uuid())",
        "INSERT INTO booking.outbox (stream, event_type, entity_type, entity_id, supply_version)"
        " VALUES ('supply', 'supply.upsert', 'hotel', gen_random_uuid(), 0)",
        "INSERT INTO booking.outbox (stream, event_type, entity_type, entity_id, supply_version)"
        " VALUES ('supply', 'supply.upsert', 'room', gen_random_uuid(), 1)",
        # a b2b event type on the supply stream, a supply event type on the b2b stream
        "INSERT INTO booking.outbox (stream, event_type, entity_type, entity_id, supply_version, departure_id)"
        " VALUES ('supply', 'departure.guaranteed', 'hotel', gen_random_uuid(), 1, gen_random_uuid())",
        "INSERT INTO booking.outbox (stream, event_type, partner_id)"
        " VALUES ('b2b', 'supply.upsert', gen_random_uuid())",
        # a b2b row with an entity
        "INSERT INTO booking.outbox (stream, event_type, partner_id, departure_id, entity_type, entity_id,"
        " supply_version) VALUES ('b2b', 'departure.guaranteed', gen_random_uuid(), gen_random_uuid(), 'hotel',"
        " gen_random_uuid(), 1)",
        # an unknown stream
        "INSERT INTO booking.outbox (stream, event_type, entity_type, entity_id, supply_version)"
        " VALUES ('catalog', 'supply.upsert', 'hotel', gen_random_uuid(), 1)",
    ]
    with pytest.raises(_Rollback):
        async with admin.transaction():
            await admin.execute(ok)
            for sql in bad:
                with pytest.raises(asyncpg.CheckViolationError):
                    async with admin.transaction():
                        await admin.execute(sql)
            raise _Rollback


@pytest.mark.parametrize(
    ("override", "constraint"),
    [
        ({"s3_key": "supply-media/hotel/elsewhere.jpg"}, "entity_media_key_chk"),
        ({"s3_key": "https://example.test/photo.jpg"}, "entity_media_key_chk"),
        ({"content_type": "image/gif"}, "entity_media_content_type_chk"),
        ({"s3_bucket": "someone-elses-bucket"}, "entity_media_bucket_chk"),
        ({"bytes": 10_485_761}, "entity_media_bytes_chk"),
        ({"bytes": 0}, "entity_media_bytes_chk"),
        ({"sha256": "A" * 64}, "entity_media_sha256_chk"),
        ({"width": 0}, "entity_media_size_chk"),
        ({"status": "deleted"}, "entity_media_status_chk"),
        ({"entity_type": "room"}, "entity_media_entity_type_chk"),
    ],
)
async def test_entity_media_checks(admin: asyncpg.Connection, override: dict[str, object], constraint: str) -> None:
    media_id, entity_id = uuid.uuid4(), uuid.uuid4()
    row: dict[str, object] = {
        "id": media_id,
        "entity_type": "hotel",
        "entity_id": entity_id,
        "s3_bucket": BUCKET,
        "content_type": "image/jpeg",
        "bytes": 1000,
        "width": 10,
        "height": 10,
        "sha256": SHA,
        "status": "active",
        "uploaded_by": "it-835-schema",
        **override,
    }
    # The derived key of the row as given, so each case breaks exactly one CHECK (the key's own cases override it).
    ext = {"image/jpeg": "jpg", "image/png": "png"}.get(str(row["content_type"]), "webp")
    row.setdefault("s3_key", f"supply-media/{row['entity_type']}/{entity_id}/{media_id}.{ext}")
    names = ", ".join(row)
    marks = ", ".join(f"${i + 1}" for i in range(len(row)))
    with pytest.raises(_Rollback):
        async with admin.transaction():
            await admin.execute(
                "INSERT INTO booking.staff_users (cis_user_id, role, active) VALUES ('it-835-schema', 'contracting',"
                " true) ON CONFLICT (cis_user_id) DO NOTHING"
            )
            with pytest.raises(asyncpg.CheckViolationError) as exc:
                await admin.execute(
                    f"INSERT INTO booking.entity_media ({names}) VALUES ({marks})",  # noqa: S608 — test columns
                    *row.values(),
                )
            assert exc.value.constraint_name == constraint
            raise _Rollback
