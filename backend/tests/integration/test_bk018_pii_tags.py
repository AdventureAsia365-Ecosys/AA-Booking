"""bk-018 (AA-877) on a real PostgreSQL: data standards DM-6.1 tags on the Booking columns.

COMMENT replaces a whole comment, so the main check is mechanical: clear every booking column comment, replay
bk-001 ... bk-017 (the comments as they were before bk-018), snapshot them, apply bk-018 and compare. bk-018 may
change exactly the columns of EXPECTED; a column that had a comment keeps that text verbatim at the end, every other
column stays byte-identical. The test then replays every file, so the session database ends as it started.

Also: the expected tags and the tags of bk-008 / 009 / 010 / 012, the candidate rule (a column named like personal data
carries a tag or an ALLOWLIST entry with a reason; the same rule as AA-CIS-Infra db/gen_schema_reference.py), known
classes only, registration and convergence on a second replay."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio

from tests.integration.conftest import apply_migrations, as_booking_owner

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

BK018 = 18

# Data standards DM-6.1 classes.
CLASSES = {
    "pii": {"contact", "name", "address", "profile", "online_id", "free_text", "travel", "payment"},
    "conf": {"commercial", "credential"},
}
TAG_RE = re.compile(r"^((?:pii|conf):[a-z_]+)(?=;|\s+[—–-]\s|$)")
ALSO_RE = re.compile(r"(?<![a-z_])((?:pii|conf):[a-z_]+)")

# relation.column -> (tag, also) set by bk-018.
EXPECTED: dict[str, tuple[str, tuple[str, ...]]] = {
    # A. personal data
    "customers.name": ("pii:name", ()),
    "customers.email": ("pii:contact", ()),
    "customers.phone": ("pii:contact", ()),
    "customers.address": ("pii:address", ()),
    "customers.gender": ("pii:profile", ()),
    "customers.marital_status": ("pii:profile", ()),
    "customers.profession": ("pii:profile", ()),
    "customers.profile_media_id": ("pii:profile", ()),
    "media_files.s3_key": ("pii:profile", ()),
    "v_customer_summary.name": ("pii:name", ()),
    "v_customer_summary.email": ("pii:contact", ()),
    "v_customer_summary.phone": ("pii:contact", ()),
    "v_customer_summary.address": ("pii:address", ()),
    "staff_users.name": ("pii:name", ()),
    "staff_users.email": ("pii:contact", ()),
    "staff_users.cis_user_id": ("pii:online_id", ()),
    "staff_users.cognito_iss": ("pii:online_id", ()),
    "staff_users.cognito_sub": ("pii:online_id", ()),
    "suppliers.contact_title": ("pii:profile", ()),
    "trip_cases.start_date": ("pii:travel", ()),
    "trip_cases.end_date": ("pii:travel", ()),
    "trip_cases.pax": ("pii:travel", ()),
    "trip_cases.pax_adults": ("pii:travel", ()),
    "trip_cases.pax_children": ("pii:travel", ()),
    "trip_cases.trip_style": ("pii:travel", ()),
    "trip_cases.inquiry_snapshot": ("pii:travel", ("pii:free_text",)),
    "trip_cases.notes": ("pii:free_text", ()),
    "trip_case_status_history.note": ("pii:free_text", ()),
    "trip_case_versions.notes": ("pii:free_text", ()),
    "trip_case_versions.title": ("pii:free_text", ()),
    "trip_case_versions.approval_note": ("pii:free_text", ()),
    "trip_case_version_itinerary.notes": ("pii:free_text", ()),
    "notifications.title": ("pii:free_text", ()),
    "notifications.message": ("pii:free_text", ("pii:name",)),
    "notifications.data": ("pii:contact", ()),
    "audit_logs.before": ("pii:free_text", ("pii:name", "pii:contact")),
    "audit_logs.after": ("pii:free_text", ("pii:name", "pii:contact")),
    "audit_logs.entity_label": ("pii:free_text", ("pii:name",)),
    "audit_logs.summary": ("pii:free_text", ("pii:name",)),
    "inbox_events.error": ("pii:free_text", ()),
    # B. credentials
    "customer_otps.otp_hash": ("conf:credential", ()),
    "customer_sessions.refresh_token_hash": ("conf:credential", ()),
    # C. commercial secrets
    "pricing_rules.target_margin": ("conf:commercial", ()),
    "pricing_rules.floor_margin": ("conf:commercial", ()),
    "pricing_rules.peak_uplift_pts": ("conf:commercial", ()),
    "pricing_rules.card_fee_pct": ("conf:commercial", ()),
    "pricing_rules.note": ("conf:commercial", ()),
    "policy_approvals.note": ("conf:commercial", ()),
    "version_item_costs.net_amount": ("conf:commercial", ()),
    "version_item_costs.supplier_note": ("conf:commercial", ()),
}

# Tagged before bk-018 (bk-008, bk-009, bk-010, bk-012); bk-018 must not touch them.
EXISTING: dict[str, str] = {
    "suppliers.contact_name": "pii:name",
    "suppliers.contact_email": "pii:contact",
    "suppliers.contact_phone": "pii:contact",
    "inbox_events.payload": "pii:contact",
    "suppliers.internal_notes": "conf:commercial",
    "supplier_contracts.payment_terms": "conf:commercial",
    "supplier_contracts.cancellation_policy": "conf:commercial",
    "supplier_contracts.notes": "conf:commercial",
    "trip_plans.default_channel": "conf:commercial",
    "trip_plans.price_hint_amount": "conf:commercial",
    "trip_plans.price_hint_currency": "conf:commercial",
    "trip_plans.price_hint_basis": "conf:commercial",
    "trip_plans.commercial_notes": "conf:commercial",
    "hotels.internal_notes": "conf:commercial",
    "hotel_room_types.internal_notes": "conf:commercial",
    "services.internal_notes": "conf:commercial",
    "supplier_rates.net_amount": "conf:commercial",
    "supplier_rates.notes": "conf:commercial",
}

# Candidate rule: whole snake_case tokens (same list as AA-CIS-Infra db/gen_schema_reference.py PII_CANDIDATE_TOKENS).
TOKENS = frozenset(
    {
        "email",
        "phone",
        "name",
        "address",
        "note",
        "notes",
        "message",
        "payload",
        "snapshot",
        "title",
        "summary",
        "label",
        "sub",
        "iss",
        "data",
        "itinerary",
    }
)
# Untagged on purpose, one reason each (mirrored in AA-CIS-Infra PII_ALLOWLIST, booking.* entries).
ALLOWLIST: dict[str, str] = {
    "countries.name": "reference data: country name",
    "hotels.name": "a hotel (business) name",
    "hotels.address": "a hotel (business) address",
    "hotel_room_types.name": "a room type name",
    "services.title": "internal working title of a supplier service (bk-010), no customer data",
    "supplier_contracts.title": "a contract title, no customer data",
    "suppliers.company_name": "a supplier company name; the contact person is suppliers.contact_name",
    "suppliers.address": "a supplier business address (AA-877 decision: a sole-trader guide's business address is "
    "covered by the supplier-contacts RoPA activity, not tagged)",
    "trip_case_version_itinerary.title": "itinerary item title (tour, hotel or service name)",
    "job.payload": "job payloads carry ids only (DM-6.3)",
}

COMMENTS_SQL = """
SELECT c.relname || '.' || a.attname AS col, col_description(c.oid, a.attnum) AS comment
  FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
 WHERE c.relnamespace = 'booking'::regnamespace AND c.relkind IN ('r', 'p', 'v', 'm')
   AND a.attnum > 0 AND NOT a.attisdropped"""

CLEAR_SQL = """
DO $$
DECLARE r record;
BEGIN
    FOR r IN SELECT c.relname, a.attname
               FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
              WHERE c.relnamespace = 'booking'::regnamespace AND c.relkind IN ('r', 'p', 'v', 'm')
                AND a.attnum > 0 AND NOT a.attisdropped
                AND col_description(c.oid, a.attnum) IS NOT NULL
    LOOP
        EXECUTE format('COMMENT ON COLUMN booking.%I.%I IS NULL', r.relname, r.attname);
    END LOOP;
END $$"""


def parse(comment: str | None) -> tuple[str | None, list[str]]:
    if not comment:
        return None, []
    m = TAG_RE.match(comment)
    tag = m.group(1) if m else None
    also: list[str] = []
    for t in ALSO_RE.findall(comment[m.end() :] if m else comment):
        if t != tag and t not in also:
            also.append(t)
    return tag, also


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def _comments(conn: asyncpg.Connection) -> dict[str, str | None]:
    return {r["col"]: r["comment"] for r in await conn.fetch(COMMENTS_SQL)}


async def test_bk018_only_prefixes_tags_and_keeps_old_text(admin: asyncpg.Connection) -> None:
    try:
        async with as_booking_owner(admin):
            await admin.execute(CLEAR_SQL)
        await apply_migrations(admin, only=range(1, BK018))
        before = await _comments(admin)
        await apply_migrations(admin, only=[BK018])
        after = await _comments(admin)
    finally:
        await apply_migrations(admin)  # the full replay: the session database ends as it started
    assert set(before) == set(after)
    changed = {col for col in after if after[col] != before[col]}
    assert changed == set(EXPECTED), (sorted(changed - set(EXPECTED)), sorted(set(EXPECTED) - changed))
    for col in sorted(changed):
        tag = EXPECTED[col][0]
        new, old = after[col], before[col]
        assert new is not None and new.startswith(f"{tag}; "), (col, new)
        if old is not None:  # a previous comment survives verbatim after the tag
            assert new.endswith(old) and len(new) > len(old) + len(tag), (col, old, new)
        else:
            assert len(new) > len(tag) + 2, (col, new)


async def test_expected_and_existing_tags(admin: asyncpg.Connection) -> None:
    comments = await _comments(admin)
    for col, (tag, also) in EXPECTED.items():
        assert parse(comments[col]) == (tag, list(also)), (col, comments[col])
    for col, tag in EXISTING.items():
        assert parse(comments[col])[0] == tag, (col, comments[col])
    assert not set(EXPECTED) & set(EXISTING)


async def test_every_tag_uses_a_known_class(admin: asyncpg.Connection) -> None:
    for col, comment in (await _comments(admin)).items():
        tag, also = parse(comment)
        if tag is None:
            assert not also, f"{col}: names {also} but does not start with a tag: {comment!r}"
        for t in ([tag] if tag else []) + also:
            kind, _, klass = t.partition(":")
            assert klass in CLASSES[kind], f"{col}: unknown class {t}"


async def test_columns_named_like_personal_data_are_tagged_or_allowlisted(admin: asyncpg.Connection) -> None:
    comments = await _comments(admin)
    untagged = {col for col, comment in comments.items() if parse(comment)[0] is None}
    candidates = {col for col in untagged if set(col.split(".", 1)[1].split("_")) & TOKENS}
    assert candidates - set(ALLOWLIST) == set(), sorted(candidates - set(ALLOWLIST))
    assert set(ALLOWLIST) - candidates == set(), f"stale allowlist entries: {sorted(set(ALLOWLIST) - candidates)}"
    assert all(reason.strip() for reason in ALLOWLIST.values())


async def test_bk018_is_registered_and_converges(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM booking.v_schema_versions WHERE version = 'bk-018'") == 1
    before = await _comments(admin)
    await apply_migrations(admin)
    assert await _comments(admin) == before
