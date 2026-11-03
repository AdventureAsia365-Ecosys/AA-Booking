"""``bk_tenant_pull`` (AA-1028 R1 slice 2, root ADR 0019 d1 / d9): ``booking.b2b_partners`` becomes a read-only
projection of the CIS tenant master, one row for **every** CIS tenant (Content-only included; AA itself, kind
``internal``, never), written only by this job.

Source: the CIS contract view ``catalog.v_tenant_account_v1`` (CIS 357, contract tenant.entitlements.v1 1.1, the view
part for Booking), read as role ``booking`` like ``catalog.v_tour_lifecycle_outbox``. No HTTP call, no M2M client and
no secret: role ``booking`` has USAGE on schema ``catalog`` (CIS 225 / 228) and SELECT on the view (357).

One run (a worker maintenance hook queues one per :data:`SLOT_SECONDS` slot while the view is readable, due now):

- the whole view (tens of tenants) against the whole table, in one transaction;
- a tenant without a row -> a row: ``agency_name`` = the tenant's name (trimmed, at most 200), ``status`` = ``active``
  when the tenant is in service (trial, active, past_due) **and** its ``trade.book`` entitlement is live, else
  ``suspended``; ``m2m_client_id`` = the AA-Partner (CRM) client (``COGNITO_M2M_CRM_CLIENT_ID``: every agency's B2B
  calls come through the CRM, d9); ``billing_currency`` USD;
- a row whose name, status or client differs, or that the job never projected (``projected_at`` NULL, bk-037) ->
  updated, ``projected_at`` = now (``billing_currency`` and ``credit_terms_ref`` are Booking's commercial terms and
  stay as staff set them; ``created_by`` / ``updated_by`` reference staff users and are left alone);
- a row whose tenant left the view (erased in CIS) -> ``suspended`` (rows are never deleted: role ``booking`` has no
  DELETE, and a quote or hold may reference the partner);
- each change writes one ``booking.audit_logs`` row (``actor_type = system``, actor ``system:bk_tenant_pull``,
  ``b2b_partner.projected`` / ``.suspended`` / ``.reactivated``).

Without the view (CIS 357 not applied, or no grant) the run answers ``skipped``; without the CRM client id
(``COGNITO_M2M_CRM_CLIENT_ID`` empty) ``not_configured``. A demo tenant (E2E, sales demo) gets a row like an agency.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import asyncpg

from app import audit
from app.catalog import liveness
from app.jobs import queue
from app.jobs.queue import Job

log = logging.getLogger(__name__)

KIND = "bk_tenant_pull"
SCHEDULER = "system:tenant_pull"
ACTOR = "system:bk_tenant_pull"  # the audit actor (booking.audit_logs.actor_ref)
MAX_ATTEMPTS = 3
SLOT_SECONDS = 300
NAME_MAX = 200
CURRENCY = "USD"

VIEW = liveness.View("catalog", "v_tenant_account_v1", ("tenant_id", "name", "in_service", "kind", "trade_book"))
ACCOUNTS_SQL = f"SELECT tenant_id, name, in_service, kind, trade_book FROM {VIEW.qualified} ORDER BY tenant_id"  # noqa: S608
PARTNERS_SQL = """
SELECT id::text AS id, tenant_id, agency_name, status, m2m_client_id, projected_at FROM booking.b2b_partners FOR UPDATE
"""
# created_by / updated_by reference booking.staff_users: the job leaves them alone and marks projected_at (bk-037).
INSERT_SQL = """
INSERT INTO booking.b2b_partners (tenant_id, agency_name, status, billing_currency, m2m_client_id, projected_at)
VALUES ($1, $2, $3, $4, $5, now())
ON CONFLICT (tenant_id) DO NOTHING
RETURNING id::text
"""
UPDATE_SQL = """
UPDATE booking.b2b_partners SET agency_name = $2, status = $3, m2m_client_id = $4, projected_at = now()
 WHERE id = $1::uuid
"""


def desired(account: dict[str, Any] | asyncpg.Record, client_id: str) -> dict[str, str]:
    """What the partner row of a tenant must hold (pure)."""
    name = " ".join(str(account["name"] or "").split())[:NAME_MAX] or "Unnamed agency"
    status = "active" if account["in_service"] and account["trade_book"] else "suspended"
    return {"agency_name": name, "status": status, "m2m_client_id": client_id}


def _action(before: dict[str, Any], after: dict[str, str]) -> str:
    if before["status"] != after["status"] and set(_diff(before, after)) == {"status"}:
        return "b2b_partner.suspended" if after["status"] == "suspended" else "b2b_partner.reactivated"
    return "b2b_partner.projected"


def _diff(before: dict[str, Any], after: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in after.items() if before.get(k) != v}


async def _audit(conn: asyncpg.Connection, partner_id: str, label: str, action: str, before: Any, after: Any) -> None:
    await audit.record(
        conn,
        actor_type="system",
        actor_ref=ACTOR,
        action=action,
        entity_type="b2b_partner",
        entity_id=partner_id,
        before=before,
        after=after,
        entity_label=label[:80],
        summary=action.replace("b2b_partner.", "B2B partner ").replace("_", " ") + " from the CIS tenant account",
    )


def build_handler(client_id: str) -> Callable[[asyncpg.Pool, Job], Any]:
    async def pull(pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
        return await run(pool, client_id)

    return pull


async def run(pool: asyncpg.Pool, client_id: str) -> dict[str, Any]:
    if not client_id:
        return {"outcome": "not_configured", "reason": "COGNITO_M2M_CRM_CLIENT_ID is empty"}
    async with pool.acquire() as conn:
        if not await liveness.view_ready(conn, VIEW):
            return {"outcome": "skipped", "reason": f"{VIEW.qualified} not readable (CIS 357)"}
    counts = {"created": 0, "updated": 0, "suspended_gone": 0, "unchanged": 0, "internal_skipped": 0}
    async with pool.acquire() as conn, conn.transaction():
        accounts = await conn.fetch(ACCOUNTS_SQL)
        partners = {r["tenant_id"]: dict(r) for r in await conn.fetch(PARTNERS_SQL)}
        seen = set()
        for account in accounts:
            tenant_id = account["tenant_id"]
            if account["kind"] == "internal":
                counts["internal_skipped"] += 1
                continue
            seen.add(tenant_id)
            want = desired(account, client_id)
            row = partners.get(tenant_id)
            if row is None:
                partner_id = await conn.fetchval(
                    INSERT_SQL, tenant_id, want["agency_name"], want["status"], CURRENCY, client_id
                )
                if partner_id is not None:  # a concurrent run inserted it first: the next run compares
                    await _audit(conn, partner_id, want["agency_name"], "b2b_partner.created", None,
                                 {"tenant_id": str(tenant_id), **want})  # fmt: skip
                    counts["created"] += 1
                continue
            changed = _diff(row, want)
            if not changed and row["projected_at"] is not None:
                counts["unchanged"] += 1
                continue
            # a row staff registered is adopted (projected_at set) even when it already holds what CIS says
            await conn.execute(UPDATE_SQL, row["id"], want["agency_name"], want["status"], want["m2m_client_id"])
            await _audit(conn, row["id"], want["agency_name"], _action(row, want), {k: row[k] for k in changed},
                         changed)  # fmt: skip
            counts["updated"] += 1
        in_cis = {a["tenant_id"] for a in accounts}
        for tenant_id, row in partners.items():
            if tenant_id in seen or row["status"] == "suspended":
                continue
            if tenant_id not in in_cis:  # gone from CIS (a row of an internal tenant stays as it is)
                await conn.execute(UPDATE_SQL, row["id"], row["agency_name"], "suspended", row["m2m_client_id"])
                await _audit(conn, row["id"], row["agency_name"], "b2b_partner.suspended", {"status": row["status"]},
                             {"status": "suspended"})  # fmt: skip
                counts["suspended_gone"] += 1
    if counts["created"] or counts["updated"] or counts["suspended_gone"]:
        log.info("bk_tenant_pull: %s", counts)
    return {"outcome": "ok", "tenants": len(accounts), **counts}


def schedule_key(at: datetime) -> str:
    slot = int(at.timestamp()) // SLOT_SECONDS
    return f"{KIND}:slot:{slot}"


class TenantPullSchedule:
    """Worker maintenance hook: one ``bk_tenant_pull`` run per :data:`SLOT_SECONDS` slot, due now, only while the CIS
    view is readable (feature detection, cached by :func:`app.catalog.liveness.view_ready`); never ahead."""

    def __init__(self, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._now = now
        self._done_slot: str | None = None

    async def __call__(self, pool: asyncpg.Pool) -> str | None:
        key = schedule_key(self._now())
        if self._done_slot == key:
            return None
        async with pool.acquire() as conn:
            if not await liveness.view_ready(conn, VIEW):
                return None
        job_id = await queue.enqueue(
            pool, KIND, {"trigger": "schedule"}, idempotency_key=key, max_attempts=MAX_ATTEMPTS, created_by=SCHEDULER
        )
        self._done_slot = key
        return job_id
