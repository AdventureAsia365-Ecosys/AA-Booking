"""One-off display fix of B2B trip cases confirmed before AA-1013 (#163) and AA-1088 (#231) (S278 lane l18, M13).

The S278 live verify found older B2B cases that still read wrong where staff look at them, although their documents
are right (the documents compute the values at read time since #231):

- **Title.** Cases confirmed before #163 have version 1 titled ``B2B <proposal ref> v<n>`` (the case header shows it)
  and items titled ``<kind> <component id> · line <line ref>``. Since #163 the confirm writes the tour sold
  (:func:`app.b2b.holds.line_titles`: the CIS tour title for a trip plan line, the hotel / service name otherwise).
- **Length.** Cases confirmed before #231 stored ``end_date`` and the items' ``day_to`` from the highest priced day
  only. Since #231 a trip plan line covers the tour's days (:mod:`app.b2b.plan_span`).

:func:`plan` computes, for every B2B case with a confirmed hold, what the confirm would write today with the same code
(``line_titles``, ``plan_span``): a new end date only when it is later than the stored one (a staff-extended end is
kept, as the documents do), a new version title only over the exact placeholder, an item title only over its exact
placeholder, a ``day_to`` only when longer. Synthetic and real agencies alike: this is display data, never money.
:func:`apply` writes it, one transaction per case, as role ``booking`` with ``booking.legacy_display_fix = 'aa1088'``
set for that transaction: the bk-003 version locks let exactly these changes through (bk-056); one audit row per case
(``trip_case.legacy_display_fixed``, actor ``system``, with the plan hash). Idempotent: a fixed case plans nothing.

**The apply writes only the plan a person reviewed.** The DRY RUN prints ``plan_hash`` (SHA-256 of every planned
change: case ids and codes, old and new end dates, titles, item spans). ``--apply`` requires ``--expect-plan <that
hash>``, computes the plan again and writes nothing (exit 3) when it differs: a case confirmed or a catalogue tour
length changed since the DRY RUN shows up as a refused apply, never as an unreviewed write.

Run in the Booking API container (``DATABASE_URL`` = role ``booking``) through ECS Exec on ``aa-booking-dev-api``::

    python -m app.b2b.legacy_cases                                   # DRY RUN: counts, the changes, plan_hash
    python -m app.b2b.legacy_cases --apply --expect-plan <plan_hash>  # write exactly that plan

Output: one JSON object (counts; per case its code, old and new end date, whether the titles change). No names of
travellers or agencies.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from collections.abc import Sequence
from typing import Any

import asyncpg

from app import audit
from app.b2b import holds, plan_span
from app.b2b.components import PREFIX_PLAN, parse_component

FIX_SETTING = "booking.legacy_display_fix"
FIX_VALUE = "aa1088"
ACTOR = "aa1088-legacy-display-fix"
REQUIRED_KEY = "bk-056"

CASES_SQL = """
SELECT tc.id::text AS case_id, tc.code, tc.start_date, tc.end_date, h.quote_id::text AS quote_id, h.proposal_ref,
       h.proposal_version, v.id::text AS version_id, v.title AS version_title, v.is_sent_to_customer AS sent
FROM booking.trip_cases tc
JOIN booking.b2b_holds h ON h.trip_case_id = tc.id
JOIN booking.trip_case_versions v ON v.trip_case_id = tc.id AND v.version_number = 1 AND v.deleted_at IS NULL
WHERE tc.channel = 'b2b' AND tc.deleted_at IS NULL
ORDER BY tc.code
"""
ITEMS_SQL = """
SELECT id::text AS id, position, title, day_from, day_to FROM booking.trip_case_version_itinerary
WHERE trip_case_version_id = $1::uuid ORDER BY position
"""
KEY_SQL = "SELECT EXISTS (SELECT 1 FROM booking.v_schema_versions WHERE version = $1)"
HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class PlanChanged(RuntimeError):
    """The plan computed at apply time is not the one the DRY RUN showed (exit 3, nothing written)."""


def plan_hash(changes: list[dict[str, Any]]) -> str:
    """SHA-256 of the planned changes in a canonical JSON form (sorted keys, dates as ISO text)."""
    canonical = json.dumps(changes, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def legacy_version_title(proposal_ref: str, proposal_version: int) -> str:
    """The version title a B2B confirm wrote before AA-1013 (Booking #163)."""
    return f"B2B {proposal_ref} v{proposal_version}"[:200]


def legacy_item_title(line: Any) -> str:
    """An item title a B2B confirm wrote before AA-1013 (Booking #163)."""
    return f"{line['component_kind']} {line['component_id']} · line {line['line_ref']}"[:500]


async def _case_plan(conn: asyncpg.Connection, case: Any) -> dict[str, Any] | None:
    lines = await conn.fetch(holds.QUOTE_LINES_SQL, case["quote_id"])
    if not lines:
        return None
    plan_ids = sorted(
        {ident for prefix, ident in (parse_component(r["component_id"]) for r in lines) if prefix == PREFIX_PLAN}
    )
    days, source = await plan_span.plan_days(conn, plan_ids)
    computed_end = plan_span.trip_end(lines, days)
    by_no = {r["line_no"]: r for r in lines}
    change: dict[str, Any] = {"case_id": case["case_id"], "code": case["code"], "version_id": case["version_id"]}
    if computed_end is not None and case["end_date"] is not None and computed_end > case["end_date"]:
        change["end_date"] = (case["end_date"], computed_end)
    titles: dict[int, str] | None = None
    if case["version_title"] == legacy_version_title(case["proposal_ref"], case["proposal_version"]):
        titles = await holds.line_titles(conn, lines, source)
        headline = next(
            (titles[r["line_no"]] for r in lines if parse_component(r["component_id"])[0] == PREFIX_PLAN),
            titles[lines[0]["line_no"]],
        )
        change["version_title"] = headline[:200]
    items: list[dict[str, Any]] = []
    start = case["start_date"]
    for item in await conn.fetch(ITEMS_SQL, case["version_id"]):
        line = by_no.get(item["position"])
        if line is None:
            continue
        fix: dict[str, Any] = {}
        if item["title"] == legacy_item_title(line):
            titles = titles if titles is not None else await holds.line_titles(conn, lines, source)
            fix["title"] = titles[line["line_no"]][:500]
        if start is not None and line["service_date"] is not None:
            day_from = (line["service_date"] - start).days + 1
            day_to = day_from + plan_span.line_span(line, days) - 1
            if item["day_from"] == day_from and day_to > item["day_to"]:
                fix["day_to"] = min(day_to, plan_span.MAX_DAYS)
        if fix:
            items.append({"id": item["id"], "position": item["position"], **fix})
    if items:
        change["items"] = items
    return change if len(change) > 3 else None


async def plan(conn: asyncpg.Connection) -> dict[str, Any]:
    """DRY RUN: every B2B case and what would change (read-only)."""
    changes: list[dict[str, Any]] = []
    sent = 0
    cases = await conn.fetch(CASES_SQL)
    for case in cases:
        change = await _case_plan(conn, case)
        if change is None:
            continue
        if case["sent"]:
            sent += 1  # a version sent to the customer stays frozen (bk-005 sent locks)
            continue
        changes.append(change)
    return {
        "b2b_cases": len(cases),
        "to_fix": len(changes),
        "end_dates": sum(1 for c in changes if "end_date" in c),
        "version_titles": sum(1 for c in changes if "version_title" in c),
        "item_titles": sum(1 for c in changes for i in c.get("items", []) if "title" in i),
        "item_spans": sum(1 for c in changes for i in c.get("items", []) if "day_to" in i),
        "skipped_sent": sent,
        "plan_hash": plan_hash(changes),
        "changes": changes,
    }


async def _apply_one(conn: asyncpg.Connection, change: dict[str, Any], digest: str) -> None:
    async with conn.transaction():
        await conn.execute("SELECT set_config($1, $2, true)", FIX_SETTING, FIX_VALUE)
        before: dict[str, Any] = {}
        after: dict[str, Any] = {}
        if "end_date" in change:
            old, new = change["end_date"]
            await conn.execute(
                "UPDATE booking.trip_cases SET end_date = $2 WHERE id = $1::uuid AND end_date = $3",
                change["case_id"],
                new,
                old,
            )
            before["end_date"], after["end_date"] = old, new
        if "version_title" in change:
            await conn.execute(
                "UPDATE booking.trip_case_versions SET title = $2 WHERE id = $1::uuid",
                change["version_id"],
                change["version_title"],
            )
            after["version_title"] = change["version_title"]
        for item in change.get("items", []):
            await conn.execute(
                "UPDATE booking.trip_case_version_itinerary SET title = coalesce($2, title),"
                " day_to = coalesce($3, day_to) WHERE id = $1::uuid",
                item["id"],
                item.get("title"),
                item.get("day_to"),
            )
        if change.get("items"):
            after["items"] = len(change["items"])
        after["plan_hash"] = digest
        await audit.record(
            conn,
            actor_type="system",
            actor_ref=ACTOR,
            action="trip_case.legacy_display_fixed",
            entity_type="trip_case",
            entity_id=change["case_id"],
            before=before or None,
            after=after,
            entity_label=change["code"],
            summary=f"Display fix of {change['code']} (AA-1088): tour title and whole-tour dates as confirmed today",
        )


async def apply(conn: asyncpg.Connection, expected_hash: str) -> dict[str, Any]:
    """Write the planned changes, one transaction per case, only when the plan is still the DRY RUN's
    (``expected_hash`` = its ``plan_hash``; else :class:`PlanChanged`, nothing written). Refuses before bk-056 (the
    locks would refuse anyway)."""
    if not HASH_RE.match(expected_hash or ""):
        raise PlanChanged("--expect-plan must be the DRY RUN's plan_hash (64 lower-case hex characters)")
    if not await conn.fetchval(KEY_SQL, REQUIRED_KEY):
        raise RuntimeError(f"{REQUIRED_KEY} is not applied: the version locks refuse the fix")
    planned = await plan(conn)
    if planned["plan_hash"] != expected_hash:
        raise PlanChanged(
            f"the plan changed since the DRY RUN (now {planned['plan_hash']}, {planned['to_fix']} cases): "
            "nothing written; run the DRY RUN again and review it"
        )
    for change in planned["changes"]:
        await _apply_one(conn, change, expected_hash)
    return planned


def _report(planned: dict[str, Any]) -> dict[str, Any]:
    def one(c: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {"code": c["code"]}
        if "end_date" in c:
            out["end_date"] = [d.isoformat() for d in c["end_date"]]
            out["days_added"] = (c["end_date"][1] - c["end_date"][0]).days
        out["version_title"] = "version_title" in c
        out["items"] = len(c.get("items", []))
        return out

    return {**{k: v for k, v in planned.items() if k != "changes"}, "cases": [one(c) for c in planned["changes"]]}


async def _main(apply_changes: bool, expected_hash: str | None) -> int:
    if apply_changes and not expected_hash:
        print(json.dumps({"error": "--apply needs --expect-plan <plan_hash of the DRY RUN>"}))
        return 2
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print(json.dumps({"error": "DATABASE_URL not set"}))
        return 2
    conn = await asyncpg.connect(
        dsn,
        ssl=os.environ.get("DB_SSL", "require"),
        timeout=10,
        server_settings={"application_name": "booking-legacy-cases"},
    )
    try:
        result = await apply(conn, expected_hash or "") if apply_changes else await plan(conn)
    except RuntimeError as exc:
        print(json.dumps({"error": str(exc)}))
        return 3
    finally:
        await conn.close()
    print(json.dumps({"mode": "APPLY" if apply_changes else "DRY RUN", **_report(result)}, indent=1, default=str))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Display fix of B2B cases confirmed before AA-1013 / AA-1088.")
    parser.add_argument("--apply", action="store_true", help="write the fix (default: DRY RUN, counts only)")
    parser.add_argument("--expect-plan", default=None, help="with --apply: the DRY RUN's plan_hash (required)")
    args = parser.parse_args(argv)
    return asyncio.run(_main(args.apply, args.expect_plan))


if __name__ == "__main__":
    sys.exit(main())
