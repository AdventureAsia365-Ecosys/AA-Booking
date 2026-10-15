"""Inquiry intake jobs (AA-681 slice 2): TripPlanner outbox → ``booking.inbox_events`` → trip case.

``bk_inquiry_pull`` (recurring, one at a time)
    Reads ``tripplanner.v_inquiry_outbox`` as role ``booking`` (grant: TripPlanner tp-009) and
    stores every row it has not consumed yet in ``booking.inbox_events`` (ON CONFLICT DO NOTHING),
    queueing one ``bk_inquiry_process`` job per new row in the same transaction. Contract rule 4:
    the producer's BIGSERIAL id is never the only cursor — the scan anti-joins the inbox, and
    ``booking.inbox_cursor.safe_id`` (a scan bound) only advances over rows old enough that no
    lower id can still commit (see :data:`SAFE_AFTER_SECONDS`).

``bk_inquiry_process`` (one per inbox row)
    Validates the payload (inquiry.submitted v1 / v1.1), then in ONE transaction, holding the inbox
    row lock: creates a lead customer (O6), the trip case and its legs, a staff notification (plus one
    ``bk_notify_email`` job for it, AA-811) and an audit row, and marks the inbox row ``processed``.
    Re-running it is a no-op. A legacy hand-off with ``customer: null`` (or a contact with neither
    email nor phone) is ``parked``; an invalid payload is ``failed`` with the reason. Neither becomes
    a trip case.

Customer rule (contract v1.1, ``verified``): an unverified contact is never merged into an existing
customer. It always becomes a new lead row (``verified = false``, ``source = 'tripplanner_lead'``);
when its email or phone matches an existing live customer the lead carries
``needs_merge_review = true`` and ``merge_candidate_id``. Only a contact marked ``verified`` whose
email matches a verified customer is linked to that customer.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import asyncpg

from app.inquiry.contract import CONTRACT, SOURCE, Contact, ContractError, Inquiry, parse, version_label
from app.jobs import queue
from app.jobs.queue import Job
from app.jobs.worker import PermanentError, Recurring
from app.notifications import staff_email

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

LEAD_SOURCE = "tripplanner_lead"

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


async def _open_trip_case(conn: asyncpg.Connection, event_id: str, inquiry: Inquiry) -> dict[str, Any]:
    contact = inquiry.contact
    assert contact is not None  # noqa: S101 — checked by the caller
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
    if inquiry.legs:
        await conn.executemany(
            "INSERT INTO booking.trip_case_legs (trip_case_id, position, source_tour_id, day_from, day_to)"
            " VALUES ($1::uuid, $2, $3, $4, $5)",
            [(case["id"], leg.position, leg.source_tour_id, leg.day_from, leg.day_to) for leg in inquiry.legs],
        )
    summary = {
        "trip_case_id": case["id"],
        "code": case["code"],
        "customer_id": customer_id,
        "lead": created_lead,
        "needs_merge_review": review,
        "legs": len(inquiry.legs),
        "contract_version": inquiry.version,
    }
    message = f"{contact.name or 'A traveller'} sent a trip with {len(inquiry.legs)} AA tour leg(s)."
    if review:
        message += " The contact matches an existing customer: review before merging."
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


async def process(pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
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
            inquiry = parse(json.loads(event["payload"]))
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
