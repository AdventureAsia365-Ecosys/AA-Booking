"""Inquiry intake jobs (AA-681 slice 2): TripPlanner outbox → ``booking.inbox_events`` → trip case.

``bk_inquiry_pull`` (recurring, one at a time)
    Reads ``tripplanner.v_inquiry_outbox`` as role ``booking`` (grant: TripPlanner tp-009) and
    stores every row it has not consumed yet in ``booking.inbox_events`` (ON CONFLICT DO NOTHING),
    queueing one ``bk_inquiry_process`` job per new row in the same transaction. Contract rule 4:
    the producer's BIGSERIAL id is never the only cursor — the scan anti-joins the inbox, and
    ``booking.inbox_cursor.safe_id`` (a scan bound) only advances over rows old enough that no
    lower id can still commit (see :data:`SAFE_AFTER_SECONDS`).

``bk_inquiry_process`` (one per inbox row)
    Validates the payload (inquiry.submitted v1 / v1.1 / v1.2 / v1.3 / v1.4), then in ONE transaction, holding the inbox
    row lock: creates a lead customer (O6), the trip case and its legs, a staff notification (plus one
    ``bk_notify_email`` job for it, AA-811) and an audit row, and marks the inbox row ``processed``.
    Re-running it is a no-op. A legacy hand-off with ``customer: null`` (or a contact with neither
    email nor phone) is ``parked``; an invalid payload is ``failed`` with the reason. Neither becomes
    a trip case. Each leg gets the catalog tour key (bk-011, AA-830): from the v1.2 payload, else by
    resolving ``source_tour_id`` against ``catalog.v_tour`` (``leg_keys.resolve``); unresolved stays null.

Customer rule (contract v1.1, ``verified``): an unverified contact is never merged into an existing
customer. It always becomes a new lead row (``verified = false``, ``source = 'tripplanner_lead'``);
when its email or phone matches an existing live customer the lead carries
``needs_merge_review = true`` and ``merge_candidate_id``. Only a contact marked ``verified`` whose
email matches a verified customer is linked to that customer.

Signed-in traveller (contract v1.4, AA-908, root ADR 0006 Phase 4; ``Contact.signed_in``: identity + verified + email):
the contact is a verified customer, found in this order (:func:`resolve_signed_in`, same transaction):
  (a) its identity ``(iss, sub)`` in ``booking.customer_identities`` → that customer (when still live);
  (b) else a verified live customer with the same email → that customer, and the identity is attached to it;
  (c) else a new customer, ``verified = true``, ``source = 'traveller_signin'``, with the name and the token's email
      only (no phone or other typed field is imported, ADR 0006 Phase 4).
The identity must come from the pinned traveller issuer (``COGNITO_EXTERNAL_ISSUER``, :func:`build_handlers`); any other
issuer, or a non-https one, fails the hand-off as a contract error instead of reaching the bk-024 shape CHECK.
Concurrent hand-offs of one identity are serialised by a transaction advisory lock on (iss, sub), so the second one
sees the first one's customer instead of failing on the key and waiting for a retry.
An identity whose customer was archived is re-pointed to the customer of (b) / (c). Then every unverified live
``tripplanner_lead`` / ``manual`` customer with the same email and no merge candidate yet is flagged
``needs_merge_review`` with ``merge_candidate_id`` = the verified customer — never merged: a lead's email was typed
by an unauthenticated guest. A blocked customer still gets the case (staff decide); the notification says so.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Collection
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING, Any

import asyncpg

from app.auth.identity_config import DEV_EXTERNAL_ISSUER
from app.inquiry import leg_keys
from app.inquiry.contract import CONTRACT, SOURCE, Contact, ContractError, Inquiry, Leg, parse, version_label
from app.jobs import queue
from app.jobs.queue import Job
from app.jobs.worker import Handler, PermanentError, Recurring
from app.notifications import staff_email

if TYPE_CHECKING:
    from app.settings import Settings

log = logging.getLogger(__name__)

PULL_KIND = "bk_inquiry_pull"
PROCESS_KIND = "bk_inquiry_process"
OUTBOX_VIEW = "tripplanner.v_inquiry_outbox"

PULL_EVERY_SECONDS = 60
PULL_BATCH = 200
# The outbox row is written inside TripPlanner's hand-off transaction, so its created_at is that
# transaction's start, while its id is drawn later and becomes visible at commit. Assuming a hand-off
# transaction lasts under MAX_PRODUCER_TX_SECONDS, every row with created_at older than twice that
# drew its id before any still-invisible row started — so its id is below every id that can still
# appear. Only such rows may move safe_id.
MAX_PRODUCER_TX_SECONDS = 300
SAFE_AFTER_SECONDS = 2 * MAX_PRODUCER_TX_SECONDS

# The traveller issuers a v1.4 identity may come from when no settings are bound (tests, HANDLERS); the worker binds
# Settings.cognito_external_issuer through build_handlers().
DEFAULT_TRAVELLER_ISSUERS = frozenset({DEV_EXTERNAL_ISSUER})

LEAD_SOURCE = "tripplanner_lead"
SIGNIN_SOURCE = "traveller_signin"  # bk-024 customers_source_chk

RECURRING = (Recurring(PULL_KIND, PULL_EVERY_SECONDS),)


# ── Pull ─────────────────────────────────────────────────────────────────────


async def pull(pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read"):
        # One snapshot for the scan and the cursor advance: a row the advance can see, the scan saw.
        await conn.execute(
            "INSERT INTO booking.inbox_cursor (source, contract) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            SOURCE,
            CONTRACT,
        )
        safe_id = await conn.fetchval(
            "SELECT safe_id FROM booking.inbox_cursor WHERE source = $1 AND contract = $2 FOR UPDATE",
            SOURCE,
            CONTRACT,
        )
        rows = await conn.fetch(
            f"""
            SELECT o.id, o.idempotency_key, o.payload::text AS payload
            FROM {OUTBOX_VIEW} o
            WHERE o.id > $1
              AND NOT EXISTS (
                  SELECT 1 FROM booking.inbox_events i
                  WHERE i.source = $2 AND i.contract = $3 AND i.idempotency_key = o.idempotency_key)
            ORDER BY o.id
            LIMIT $4
            """,  # noqa: S608 — OUTBOX_VIEW is a module constant, not input
            safe_id,
            SOURCE,
            CONTRACT,
            PULL_BATCH,
        )
        inserted = 0
        for row in rows:
            payload = row["payload"]
            event_id = await conn.fetchval(
                """
                INSERT INTO booking.inbox_events
                    (source, contract, idempotency_key, payload, contract_version, source_position)
                VALUES ($1, $2, $3, $4::jsonb, $5, $6)
                ON CONFLICT (source, contract, idempotency_key) DO NOTHING
                RETURNING id::text
                """,
                SOURCE,
                CONTRACT,
                row["idempotency_key"],
                payload,
                version_label(json.loads(payload)),
                row["id"],
            )
            if event_id is None:
                continue
            inserted += 1
            await queue.enqueue(
                conn, PROCESS_KIND, {"inbox_event_id": event_id}, idempotency_key=f"{PROCESS_KIND}:{event_id}"
            )
        drained = len(rows) < PULL_BATCH
        if drained:
            safe_id = await conn.fetchval(
                f"""
                UPDATE booking.inbox_cursor SET safe_id = greatest(safe_id, coalesce((
                    SELECT max(o.id) FROM {OUTBOX_VIEW} o
                    WHERE o.id > $3 AND o.created_at < now() - make_interval(secs => $4)), 0))
                WHERE source = $1 AND contract = $2
                RETURNING safe_id
                """,  # noqa: S608 — module constant
                SOURCE,
                CONTRACT,
                safe_id,
                SAFE_AFTER_SECONDS,
            )
    if inserted:
        log.info("inquiry pull: %d new of %d scanned (safe_id=%s)", inserted, len(rows), safe_id)
    return {"scanned": len(rows), "inserted": inserted, "drained": drained, "safe_id": safe_id}


# ── Process ──────────────────────────────────────────────────────────────────


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


async def _mark(conn: asyncpg.Connection, event_id: str, status: str, error: str | None = None) -> None:
    await conn.execute(
        "UPDATE booking.inbox_events SET status = $2, error = $3, processed_at = now() WHERE id = $1::uuid",
        event_id,
        status,
        error,
    )


async def resolve_customer(conn: asyncpg.Connection, contact: Contact) -> tuple[str, bool, bool]:
    """Return (customer_id, created_lead, needs_merge_review). Never merges an unverified contact."""
    if contact.verified and contact.email:
        linked = await conn.fetchval(
            "SELECT id::text FROM booking.customers WHERE verified AND deleted_at IS NULL AND lower(email) = $1",
            contact.email,
        )
        if linked:
            return linked, False, False
    candidate = await conn.fetchval(
        """
        SELECT id::text FROM booking.customers
        WHERE deleted_at IS NULL
          AND (($1::text IS NOT NULL AND lower(email) = $1) OR ($2::text IS NOT NULL AND phone = $2))
        ORDER BY verified DESC, created_at
        LIMIT 1
        """,
        contact.email,
        contact.phone,
    )
    lead_id = await conn.fetchval(
        """
        INSERT INTO booking.customers
            (name, email, phone, verified, source, needs_merge_review, merge_candidate_id,
             external_ref, contact_consent, contact_consent_at)
        VALUES ($1, $2, $3, false, $4, $5, $6::uuid, $7, $8, $9)
        RETURNING id::text
        """,
        contact.name or None,
        contact.email,
        contact.phone,
        LEAD_SOURCE,
        candidate is not None,
        candidate,
        contact.external_ref,
        contact.consent,
        _ts(contact.consent_at),
    )
    return lead_id, True, candidate is not None


async def resolve_signed_in(conn: asyncpg.Connection, contact: Contact) -> tuple[str, str, int]:
    """Return (customer_id, how, leads_flagged) for a signed-in contact; ``how`` is identity | email | created.

    Caller guarantees ``contact.signed_in``. Runs inside the intake transaction. Logs nothing about the identity or the
    email (DM-6.4)."""
    identity, email = contact.identity, contact.email
    assert identity is not None and email  # noqa: S101 — Contact.signed_in, checked by the caller
    # Two hand-offs of one new identity at the same time: the second waits here, then sees the first one's row.
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1::text || ' ' || $2::text, 908))", identity.iss, identity.sub
    )
    known = await conn.fetchrow(
        """
        SELECT ci.customer_id::text AS customer_id, c.deleted_at IS NULL AS live
        FROM booking.customer_identities ci
        JOIN booking.customers c ON c.id = ci.customer_id
        WHERE ci.iss = $1 AND ci.sub = $2
        FOR UPDATE OF ci
        """,
        identity.iss,
        identity.sub,
    )
    if known is not None and known["live"]:
        customer_id, how = known["customer_id"], "identity"
    else:
        customer_id = await conn.fetchval(
            "SELECT id::text FROM booking.customers WHERE verified AND deleted_at IS NULL AND lower(email) = $1",
            email,
        )
        how = "email"
        if customer_id is None:
            customer_id = await conn.fetchval(
                """
                INSERT INTO booking.customers
                    (name, email, verified, source, external_ref, contact_consent, contact_consent_at)
                VALUES ($1, $2, true, $3, $4, $5, $6)
                RETURNING id::text
                """,
                contact.name or None,
                email,
                SIGNIN_SOURCE,
                contact.external_ref,
                contact.consent,
                _ts(contact.consent_at),
            )
            how = "created"
        if known is None:
            await conn.execute(
                "INSERT INTO booking.customer_identities (iss, sub, customer_id) VALUES ($1, $2, $3::uuid)",
                identity.iss,
                identity.sub,
                customer_id,
            )
        else:  # its customer was archived: the identity now signs in as this one
            await conn.execute(
                "UPDATE booking.customer_identities SET customer_id = $3::uuid WHERE iss = $1 AND sub = $2",
                identity.iss,
                identity.sub,
                customer_id,
            )
    flagged = await conn.execute(
        """
        UPDATE booking.customers
        SET needs_merge_review = true, merge_candidate_id = $1::uuid
        WHERE NOT verified AND deleted_at IS NULL AND lower(email) = $2 AND id <> $1::uuid
          AND merge_candidate_id IS NULL AND source = ANY($3::text[])
        """,
        customer_id,
        email,
        [LEAD_SOURCE, "manual"],
    )
    return customer_id, how, _updated(flagged)


def _updated(status: str) -> int:
    """Row count of an asyncpg UPDATE status ("UPDATE 3" -> 3; anything else -> 0)."""
    verb, _, count = (status or "").partition(" ")
    return int(count) if verb == "UPDATE" and count.isdigit() else 0


async def tour_keys(conn: asyncpg.Connection, legs: tuple[Leg, ...]) -> dict[int, leg_keys.TourKey]:
    """Catalog tour key per leg position: the payload's (v1.2), else ``source_tour_id`` resolved against the catalog."""
    out: dict[int, leg_keys.TourKey] = {
        leg.position: (leg.source_system, leg.source_id) for leg in legs if leg.source_system and leg.source_id
    }
    missing = [leg for leg in legs if leg.position not in out]
    if missing:
        resolved = await leg_keys.resolve(conn, [leg.source_tour_id for leg in missing])
        out.update({leg.position: resolved[leg.source_tour_id] for leg in missing if leg.source_tour_id in resolved})
    return out


def unavailable_legs(legs: tuple[Leg, ...]) -> dict[str, int]:
    """Count per reason of the legs whose tour AA no longer serves (v1.3, AA-986 L6); empty when every leg is served.

    A leg flagged unavailable without a reason counts as ``not_served``."""
    counts: dict[str, int] = {}
    for leg in legs:
        if not leg.available:
            reason = leg.reason or "not_served"
            counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


def unavailable_note(counts: dict[str, int]) -> str:
    """One staff-facing sentence: "1 leg is a tour AA no longer offers (retired 1): check it before quoting."."""
    total = sum(counts.values())
    detail = ", ".join(f"{reason.replace('_', ' ')} {n}" for reason, n in counts.items())
    if total == 1:
        return f"1 leg is a tour AA no longer offers ({detail}): check it before quoting."
    return f"{total} legs are tours AA no longer offers ({detail}): check them before quoting."


async def _open_trip_case(conn: asyncpg.Connection, event_id: str, inquiry: Inquiry) -> dict[str, Any]:
    contact = inquiry.contact
    assert contact is not None  # noqa: S101 — checked by the caller
    signed_in: dict[str, Any] = {}
    if contact.signed_in:
        customer_id, how, flagged = await resolve_signed_in(conn, contact)
        created_lead, review = False, False
        blocked = await conn.fetchval("SELECT is_blocked FROM booking.customers WHERE id = $1::uuid", customer_id)
        signed_in = {"signed_in": how, "leads_flagged_for_review": flagged, "customer_blocked": blocked is True}
    else:
        customer_id, created_lead, review = await resolve_customer(conn, contact)
    case = await conn.fetchrow(
        """
        INSERT INTO booking.trip_cases
            (customer_id, source_system, source_id, idempotency_key, inbox_event_id, inquiry_snapshot)
        VALUES ($1::uuid, 'tripplanner', $2, $3, $4::uuid, $5::jsonb)
        RETURNING id::text, code
        """,
        customer_id,
        inquiry.trip_id,
        inquiry.idempotency_key,
        event_id,
        json.dumps(inquiry.snapshot),
    )
    keyed = 0
    if inquiry.legs:
        keys = await tour_keys(conn, inquiry.legs)
        keyed = sum(1 for leg in inquiry.legs if keys.get(leg.position))
        await conn.executemany(
            "INSERT INTO booking.trip_case_legs"
            " (trip_case_id, position, source_tour_id, day_from, day_to, source_system, source_id)"
            " VALUES ($1::uuid, $2, $3, $4, $5, $6, $7)",
            [
                (
                    case["id"],
                    leg.position,
                    leg.source_tour_id,
                    leg.day_from,
                    leg.day_to,
                    *(keys.get(leg.position) or (None, None)),
                )
                for leg in inquiry.legs
            ],
        )
    unavailable = unavailable_legs(inquiry.legs)
    summary = {
        "trip_case_id": case["id"],
        "code": case["code"],
        "customer_id": customer_id,
        "lead": created_lead,
        "needs_merge_review": review,
        "legs": len(inquiry.legs),
        "legs_without_tour_key": len(inquiry.legs) - keyed,
        "legs_unavailable": unavailable,
        "contract_version": inquiry.version,
        **signed_in,
    }
    message = f"{contact.name or 'A traveller'} sent a trip with {len(inquiry.legs)} AA tour leg(s)."
    if signed_in:
        message += " Signed-in traveller (verified email)."
        if signed_in["customer_blocked"]:
            message += " Customer is blocked."
    if unavailable:
        message += " " + unavailable_note(unavailable)
    if review:
        message += " The contact matches an existing customer: review before merging."
    if signed_in.get("leads_flagged_for_review"):
        message += (
            f" {signed_in['leads_flagged_for_review']} earlier unverified lead(s) with the same email are marked for"
            " merge review."
        )
    notification_id = await conn.fetchval(
        """
        INSERT INTO booking.notifications
            (recipient_type, recipient_id, type, title, message, entity_type, entity_id, data)
        VALUES ('staff', NULL, 'trip_case.new_inquiry', $1, $2, 'trip_case', $3::uuid, $4::jsonb)
        RETURNING id::text
        """,
        f"New inquiry {case['code']}",
        message,
        case["id"],
        json.dumps(summary),
    )
    # AA-811: the staff e-mail goes out from its own job (never blocks or fails the intake); one per notification.
    await queue.enqueue(
        conn,
        staff_email.KIND,
        staff_email.payload(notification_id),
        idempotency_key=staff_email.idempotency_key(notification_id),
    )
    await conn.execute(
        """
        INSERT INTO booking.audit_logs (actor_type, action, entity_type, entity_id, after)
        VALUES ('system', 'trip_case.opened_from_inquiry', 'trip_case', $1::uuid, $2::jsonb)
        """,
        case["id"],
        json.dumps({**summary, "idempotency_key": inquiry.idempotency_key}),
    )
    return summary


async def process(
    pool: asyncpg.Pool, job: Job, *, traveller_issuers: Collection[str] = DEFAULT_TRAVELLER_ISSUERS
) -> dict[str, Any]:
    event_id = job.payload.get("inbox_event_id")
    if not isinstance(event_id, str):
        raise PermanentError("payload.inbox_event_id missing")
    async with pool.acquire() as conn, conn.transaction():
        event = await conn.fetchrow(
            "SELECT id::text, status, payload::text AS payload FROM booking.inbox_events"
            " WHERE id = $1::uuid FOR UPDATE",
            event_id,
        )
        if event is None:
            raise PermanentError(f"inbox event {event_id} not found")
        if event["status"] != "received":
            return {"outcome": "noop", "status": event["status"]}
        try:
            inquiry = parse(json.loads(event["payload"]), traveller_issuers=traveller_issuers)
        except ContractError as exc:
            await _mark(conn, event_id, "failed", str(exc)[:2000])
            log.warning("inquiry %s rejected: %s", event_id, exc)
            return {"outcome": "failed", "error": str(exc)[:500]}
        if inquiry.contact is None:
            await _mark(conn, event_id, "parked", "legacy guest hand-off: customer is null (O6)")
            return {"outcome": "parked", "reason": "customer_null"}
        if not inquiry.contact.email and not inquiry.contact.phone:
            await _mark(conn, event_id, "parked", "customer has neither email nor phone")
            return {"outcome": "parked", "reason": "no_contact"}
        existing = await conn.fetchval(
            "SELECT id::text FROM booking.trip_cases WHERE idempotency_key = $1", inquiry.idempotency_key
        )
        if existing:
            await _mark(conn, event_id, "processed")
            return {"outcome": "noop", "trip_case_id": existing}
        summary = await _open_trip_case(conn, event_id, inquiry)
        await _mark(conn, event_id, "processed")
    log.info(
        "inquiry %s -> trip case %s (lead=%s, needs_merge_review=%s)",
        event_id,
        summary["code"],
        summary["lead"],
        summary["needs_merge_review"],
    )
    return {"outcome": "processed", **summary}


HANDLERS = {PULL_KIND: pull, PROCESS_KIND: process}


def build_handlers(settings: Settings) -> dict[str, Handler]:
    """The intake handlers with the traveller issuer pinned to ``COGNITO_EXTERNAL_ISSUER`` (what the worker runs)."""
    return {
        PULL_KIND: pull,
        PROCESS_KIND: partial(process, traveller_issuers=frozenset({settings.cognito_external_issuer})),
    }
