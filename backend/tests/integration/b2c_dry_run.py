"""AA-1037 DRY RUN (read-only): what retiring the B2C half of AA-Booking would delete on Dev.

Skill aa-cis-schema references/destructive-ops.md step 1 (DRY RUN), data standards DM-3.8 / DM-3.9. Nothing is
written: the script runs SELECTs on the catalog, pg_stat_user_tables and count(*) / max(<write time>) on the B2C
tables. The output is counts, object names, enum-like codes (status, source, kind) and timestamps only, never a row
value of a personal column (personal columns appear only inside count()).

How to run it (either way, same file):
  * S3-mediated ECS exec in the aa-booking-dev-api container (preferred: role booking, the role that owns the reads):
    `python3 /tmp/s.py` uses the container env DATABASE_URL; DB_SSL=disable only for a local database.
  * AA-CIS-Infra DB Migrate mode=run-script (master login): copy to db/scripts/, the runner calls main(conn).

Output: one line per section, prefix `DRYRUN`, then one JSON line `DRYRUN_JSON {...}` with everything, and one
`DRYRUN_PINS {...}` line: for every table the draft migration (../migrations/bk_NNN_aa1037_retire_b2c.sql) drops,
its row count `n` and `md5`, an md5 over the NON-personal columns of every row (PIN_COLUMNS: key, write times,
status-like codes; one jsonb array per row, sorted, newline-joined), ready to paste into the migration's `pins` jsonb.
The migration refuses to run when a count or a hash moved since the DRY RUN, so an app UPDATE (its write time moves)
or a delete-plus-insert (the id and created_at move) after the approval also stops it. No personal column is read,
not even into the hash (the pilot PII guard refuses whole-row reads, AA-969); the session settings that shape the text
of a timestamp (TimeZone, DateStyle, IntervalStyle, extra_float_digits, bytea_output) are set to the same values here
and in the migration.

What the migration drops (pinned, rows lost or archived): customers, customer_identities, customer_otps,
customer_sessions, media_files; and invoices, invoice_items, payment_schedule, payments, whose rows it first copies
into booking.retired_b2c_record (accounting exception, DM-7.1). What it keeps (counted under `kept`, not pinned):
the b2c trip cases and everything about them, the inquiry inbox rows, notifications, audit rows.

Definitions (the 30-day guard's LAST_WRITE_SQL is the migration's "last-write" block, byte for byte except the
INTO; test_bk_b2c_drop_draft.py checks they agree, and that PIN_TABLES equals the migration's pin_tables):
  b2c case            booking.trip_cases.channel = 'b2c' (soft-deleted ones included)
  b2c version         a trip_case_versions row of a b2c case
  b2c supplier booking  supplier_bookings.trip_case_id is a b2c case
  b2c supplier bill   supplier_bills.trip_case_id is a b2c case, or a bill line is on a b2c supplier booking
  b2c payout          a supplier_payouts row of a b2c supplier bill
  b2c ops file        ops_files.trip_case_id is a b2c case, or its entity is a b2c supplier bill / payout,
                      or entity_type = 'payment'
  inquiry inbox row   inbox_events.contract = 'inquiry.submitted' (the B2C hand-off; source 'cis' rows are the
                      catalog lifecycle feed)
"""
# ruff: noqa: S608 — every SQL string is a constant of this file (no input reaches it)

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any

READ_ONLY = True

# Objects the draft migration drops (tables, then views) and the ones it rewrites in place.
DROP_TABLES = (
    "booking.payments",
    "booking.payment_schedule",
    "booking.invoice_items",
    "booking.invoices",
    "booking.customer_identities",
    "booking.customer_otps",
    "booking.customer_sessions",
    "booking.customers",
    "booking.media_files",
)
DROP_VIEWS = (
    "booking.v_invoice_balance",
    "booking.v_payment_schedule_status",
    "booking.v_customer_summary",
    "booking.v_ops_inquiry_inbox",
)
REWRITTEN_VIEWS = ("booking.v_trip_check_board", "booking.v_ops_data_totals")
REWRITTEN_FUNCTIONS = ("booking.trip_cases_post_sale_guard()", "booking.ops_files_guard()")
DROP_SEQUENCES = ("booking.customer_code_seq", "booking.invoice_number_seq", "booking.payment_ref_seq")
# Job kinds of the B2C half: the inquiry intake (the migration refuses while one is queued or running).
B2C_JOB_KINDS = ("bk_inquiry_pull", "bk_inquiry_process")
# Kinds that also served B2C (reminders of B2C invoices, customer mails); reported, not judged.
MIXED_JOB_KINDS = ("bk_ops_reminders", "bk_notify_email", "bk_case_lifecycle")

B2C_CASES = "SELECT id FROM booking.trip_cases WHERE channel = 'b2c'"
B2C_VERSIONS = f"SELECT id FROM booking.trip_case_versions WHERE trip_case_id IN ({B2C_CASES})"
B2C_SB = f"SELECT id FROM booking.supplier_bookings WHERE trip_case_id IN ({B2C_CASES})"
B2C_BILLS = (
    f"SELECT id FROM booking.supplier_bills WHERE trip_case_id IN ({B2C_CASES})"
    f" UNION SELECT bill_id FROM booking.supplier_bill_items WHERE supplier_booking_id IN ({B2C_SB})"
)
B2C_PAYOUTS = f"SELECT id FROM booking.supplier_payouts WHERE bill_id IN ({B2C_BILLS})"
B2C_FILES = (
    f"SELECT id FROM booking.ops_files WHERE trip_case_id IN ({B2C_CASES})"
    f" OR entity_type = 'payment'"
    f" OR (entity_type = 'supplier_booking' AND entity_id IN ({B2C_SB}))"
    f" OR (entity_type IN ('supplier_bill', 'supplier_payout') AND entity_id IN ({B2C_BILLS} UNION {B2C_PAYOUTS}))"
)

# The tables the migration drops, in the migration's pin_tables order; each is pinned by row count and row hash.
PIN_TABLES = (
    "customers",
    "customer_identities",
    "customer_otps",
    "customer_sessions",
    "media_files",
    "invoices",
    "invoice_items",
    "payment_schedule",
    "payments",
)
# The pinned columns of each table: its key, its write times and its status-like codes, never a personal column
# (the pilot PII guard, .claude/hooks/pilot_pii_guard.py, refuses a whole-row read of a table with `pii:` columns).
# Byte-equal to the migration's pin-columns block (test_bk_b2c_drop_draft.py checks it). An app write moves a write
# time (set_updated_at, or a new id / created_at for a delete plus insert), so the pin still sees it; a hand UPDATE of
# a personal column alone with the triggers off does not move the pin (known limit: only bk_owner can do that, and the
# rows are dropped anyway).
PIN_COLUMNS: dict[str, str] = {
    "customers": "t.id, t.code, t.source, t.verified, t.is_blocked, t.created_at, t.updated_at, t.deleted_at",
    "customer_identities": "t.customer_id, t.created_at",
    "customer_otps": "t.id, t.customer_id, t.channel, t.attempts, t.created_at, t.expires_at, t.used_at",
    "customer_sessions": "t.id, t.customer_id, t.created_at, t.expires_at, t.revoked_at",
    "media_files": "t.id, t.content_type, t.created_at, t.deleted_at",
    "invoices": (
        "t.id, t.invoice_number, t.trip_case_id, t.version_id, t.document_type, t.status, t.currency, t.total_amount,"
        " t.issued_at, t.cancelled_at, t.created_at, t.updated_at, t.deleted_at"
    ),
    "invoice_items": "t.id, t.invoice_id, t.position, t.kind, t.amount, t.created_at, t.updated_at",
    "payment_schedule": "t.id, t.invoice_id, t.seq, t.kind, t.amount, t.due_date, t.created_at, t.updated_at",
    "payments": (
        "t.id, t.ref, t.invoice_id, t.kind, t.amount, t.currency, t.method, t.recorded_at, t.verified_at,"
        " t.voided_at, t.created_at, t.updated_at"
    ),
}
# How a pin is computed (the migration's EXECUTE format is the same text with %s / %I for {cols} / {table}).
PIN_SQL_TEMPLATE = (
    "SELECT count(*) AS n, md5(coalesce(string_agg(r, chr(10) ORDER BY r), '')) AS h"
    " FROM (SELECT jsonb_build_array({cols})::text AS r FROM booking.{table} t) s"
)
# The nine pin statements written out (no run-time hole, so the PII guard reads each column list against its table);
# test_bk_b2c_drop_draft.py checks each equals PIN_SQL_TEMPLATE over PIN_COLUMNS.
PIN_SQL: dict[str, str] = {
    "customers": (
        "SELECT count(*) AS n, md5(coalesce(string_agg(r, chr(10) ORDER BY r), '')) AS h"
        " FROM (SELECT jsonb_build_array(t.id, t.code, t.source, t.verified, t.is_blocked, t.created_at, t.updated_at,"
        " t.deleted_at)::text AS r FROM booking.customers t) s"
    ),
    "customer_identities": (
        "SELECT count(*) AS n, md5(coalesce(string_agg(r, chr(10) ORDER BY r), '')) AS h"
        " FROM (SELECT jsonb_build_array(t.customer_id, t.created_at)::text AS r FROM booking.customer_identities t) s"
    ),
    "customer_otps": (
        "SELECT count(*) AS n, md5(coalesce(string_agg(r, chr(10) ORDER BY r), '')) AS h"
        " FROM (SELECT jsonb_build_array(t.id, t.customer_id, t.channel, t.attempts, t.created_at, t.expires_at,"
        " t.used_at)::text AS r FROM booking.customer_otps t) s"
    ),
    "customer_sessions": (
        "SELECT count(*) AS n, md5(coalesce(string_agg(r, chr(10) ORDER BY r), '')) AS h"
        " FROM (SELECT jsonb_build_array(t.id, t.customer_id, t.created_at, t.expires_at, t.revoked_at)::text AS r"
        " FROM booking.customer_sessions t) s"
    ),
    "media_files": (
        "SELECT count(*) AS n, md5(coalesce(string_agg(r, chr(10) ORDER BY r), '')) AS h"
        " FROM (SELECT jsonb_build_array(t.id, t.content_type, t.created_at, t.deleted_at)::text AS r"
        " FROM booking.media_files t) s"
    ),
    "invoices": (
        "SELECT count(*) AS n, md5(coalesce(string_agg(r, chr(10) ORDER BY r), '')) AS h"
        " FROM (SELECT jsonb_build_array(t.id, t.invoice_number, t.trip_case_id, t.version_id, t.document_type,"
        " t.status, t.currency, t.total_amount, t.issued_at, t.cancelled_at, t.created_at, t.updated_at,"
        " t.deleted_at)::text AS r FROM booking.invoices t) s"
    ),
    "invoice_items": (
        "SELECT count(*) AS n, md5(coalesce(string_agg(r, chr(10) ORDER BY r), '')) AS h"
        " FROM (SELECT jsonb_build_array(t.id, t.invoice_id, t.position, t.kind, t.amount, t.created_at,"
        " t.updated_at)::text AS r FROM booking.invoice_items t) s"
    ),
    "payment_schedule": (
        "SELECT count(*) AS n, md5(coalesce(string_agg(r, chr(10) ORDER BY r), '')) AS h"
        " FROM (SELECT jsonb_build_array(t.id, t.invoice_id, t.seq, t.kind, t.amount, t.due_date, t.created_at,"
        " t.updated_at)::text AS r FROM booking.payment_schedule t) s"
    ),
    "payments": (
        "SELECT count(*) AS n, md5(coalesce(string_agg(r, chr(10) ORDER BY r), '')) AS h"
        " FROM (SELECT jsonb_build_array(t.id, t.ref, t.invoice_id, t.kind, t.amount, t.currency, t.method,"
        " t.recorded_at, t.verified_at, t.voided_at, t.created_at, t.updated_at)::text AS r"
        " FROM booking.payments t) s"
    ),
}
# Row text settings, the same SET LOCALs as the migration.
HASH_SETTINGS = (
    "SET LOCAL TimeZone = 'UTC'",
    "SET LOCAL DateStyle = 'ISO, YMD'",
    "SET LOCAL IntervalStyle = 'postgres'",
    "SET LOCAL extra_float_digits = 1",
    "SET LOCAL bytea_output = 'hex'",
)

# What stays (counted for the inventory, not pinned): the b2c case graph and the rows about B2C objects.
KEPT: dict[str, str] = {
    "b2c_trip_cases": f"SELECT count(*) FROM ({B2C_CASES}) x",
    "b2c_versions": f"SELECT count(*) FROM ({B2C_VERSIONS}) x",
    "b2c_supplier_bookings": f"SELECT count(*) FROM ({B2C_SB}) x",
    "b2c_supplier_bills": f"SELECT count(*) FROM ({B2C_BILLS}) x",
    "b2c_supplier_payouts": f"SELECT count(*) FROM ({B2C_PAYOUTS}) x",
    "b2c_ops_files": f"SELECT count(*) FROM ({B2C_FILES}) x",
    "inquiry_inbox_events": "SELECT count(*) FROM booking.inbox_events WHERE contract = 'inquiry.submitted'",
}

# Latest write time of every B2C row: the migration's "last-write" block (SELECT ... INTO last_at there).
LAST_WRITE_SQL = """
    SELECT max(t) AS last_write FROM (
        SELECT greatest(max(created_at), max(updated_at)) AS t FROM booking.customers
        UNION ALL SELECT max(created_at) FROM booking.customer_identities
        UNION ALL SELECT greatest(max(created_at), max(used_at)) FROM booking.customer_otps
        UNION ALL SELECT greatest(max(created_at), max(revoked_at)) FROM booking.customer_sessions
        UNION ALL SELECT greatest(max(created_at), max(deleted_at)) FROM booking.media_files
        UNION ALL SELECT greatest(max(created_at), max(updated_at)) FROM booking.invoices
        UNION ALL SELECT greatest(max(created_at), max(updated_at)) FROM booking.invoice_items
        UNION ALL SELECT greatest(max(created_at), max(updated_at)) FROM booking.payment_schedule
        UNION ALL SELECT greatest(max(created_at), max(updated_at)) FROM booking.payments
        UNION ALL SELECT greatest(max(created_at), max(updated_at)) FROM booking.trip_cases WHERE channel = 'b2c'
        UNION ALL SELECT greatest(max(created_at), max(updated_at)) FROM booking.trip_case_versions
                   WHERE trip_case_id IN (SELECT id FROM booking.trip_cases WHERE channel = 'b2c')
        UNION ALL SELECT greatest(max(created_at), max(updated_at)) FROM booking.supplier_bookings
                   WHERE trip_case_id IN (SELECT id FROM booking.trip_cases WHERE channel = 'b2c')
        UNION ALL SELECT greatest(max(created_at), max(updated_at)) FROM booking.supplier_bills
                   WHERE trip_case_id IN (SELECT id FROM booking.trip_cases WHERE channel = 'b2c')
                      OR id IN (SELECT it.bill_id FROM booking.supplier_bill_items it
                                  JOIN booking.supplier_bookings sb ON sb.id = it.supplier_booking_id
                                  JOIN booking.trip_cases tc ON tc.id = sb.trip_case_id WHERE tc.channel = 'b2c')
        UNION ALL SELECT max(it.created_at) FROM booking.supplier_bill_items it
                   JOIN booking.supplier_bookings sb ON sb.id = it.supplier_booking_id
                   JOIN booking.trip_cases tc ON tc.id = sb.trip_case_id WHERE tc.channel = 'b2c'
        UNION ALL SELECT greatest(max(o.created_at), max(o.updated_at)) FROM booking.supplier_payouts o
                   WHERE o.bill_id IN (SELECT b.id FROM booking.supplier_bills b
                                         JOIN booking.trip_cases tc ON tc.id = b.trip_case_id WHERE tc.channel = 'b2c'
                                       UNION SELECT it.bill_id FROM booking.supplier_bill_items it
                                         JOIN booking.supplier_bookings sb ON sb.id = it.supplier_booking_id
                                         JOIN booking.trip_cases tc ON tc.id = sb.trip_case_id
                                        WHERE tc.channel = 'b2c')
        UNION ALL SELECT greatest(max(f.created_at), max(f.updated_at), max(f.removed_at)) FROM booking.ops_files f
                   WHERE f.entity_type = 'payment'
                      OR f.trip_case_id IN (SELECT id FROM booking.trip_cases WHERE channel = 'b2c')
                      OR (f.entity_type IN ('supplier_bill', 'supplier_payout') AND f.entity_id IN (
                            SELECT b.id FROM booking.supplier_bills b
                              JOIN booking.trip_cases tc ON tc.id = b.trip_case_id WHERE tc.channel = 'b2c'
                            UNION SELECT it.bill_id FROM booking.supplier_bill_items it
                              JOIN booking.supplier_bookings sb ON sb.id = it.supplier_booking_id
                              JOIN booking.trip_cases tc ON tc.id = sb.trip_case_id WHERE tc.channel = 'b2c'
                            UNION SELECT o.id FROM booking.supplier_payouts o WHERE o.bill_id IN (
                              SELECT b.id FROM booking.supplier_bills b
                                JOIN booking.trip_cases tc ON tc.id = b.trip_case_id WHERE tc.channel = 'b2c'
                              UNION SELECT it.bill_id FROM booking.supplier_bill_items it
                                JOIN booking.supplier_bookings sb ON sb.id = it.supplier_booking_id
                                JOIN booking.trip_cases tc ON tc.id = sb.trip_case_id WHERE tc.channel = 'b2c')))
        UNION ALL SELECT max(received_at) FROM booking.inbox_events WHERE contract = 'inquiry.submitted'
    ) w
"""
GUARD_DAYS = 30

BREAKDOWNS: dict[str, str] = {
    "customers_by_source": "SELECT source AS k, count(*) AS n FROM booking.customers GROUP BY 1 ORDER BY 1",
    "customers_flags": """
        SELECT 'verified' AS k, count(*) FILTER (WHERE verified) AS n FROM booking.customers
        UNION ALL SELECT 'soft_deleted', count(deleted_at) FROM booking.customers
        UNION ALL SELECT 'needs_merge_review', count(*) FILTER (WHERE needs_merge_review) FROM booking.customers
        UNION ALL SELECT 'with_merge_candidate', count(merge_candidate_id) FROM booking.customers
        UNION ALL SELECT 'with_profile_media', count(profile_media_id) FROM booking.customers
        UNION ALL SELECT 'contact_consent', count(*) FILTER (WHERE contact_consent) FROM booking.customers
        UNION ALL SELECT 'blocked', count(*) FILTER (WHERE is_blocked) FROM booking.customers""",
    "customer_sessions_live": """
        SELECT 'live' AS k, count(*) FILTER (WHERE revoked_at IS NULL AND expires_at > now()) AS n
          FROM booking.customer_sessions""",
    "trip_cases_by_channel": "SELECT channel AS k, count(*) AS n FROM booking.trip_cases GROUP BY 1 ORDER BY 1",
    "b2c_cases_by_status": """
        SELECT status || CASE WHEN deleted_at IS NULL THEN '' ELSE ' (soft-deleted)' END AS k, count(*) AS n
          FROM booking.trip_cases WHERE channel = 'b2c' GROUP BY 1 ORDER BY 1""",
    "b2c_cases_by_source": """
        SELECT source_system AS k, count(*) AS n FROM booking.trip_cases WHERE channel = 'b2c' GROUP BY 1 ORDER BY 1""",
    "b2c_cases_with_inbox_event": """
        SELECT 'with_inbox_event' AS k, count(inbox_event_id) AS n FROM booking.trip_cases WHERE channel = 'b2c'""",
    "b2b_cases_with_customer": """
        SELECT 'b2b_with_customer_id' AS k, count(customer_id) AS n FROM booking.trip_cases WHERE channel = 'b2b'""",
    "invoices_by_status": """
        SELECT status || CASE WHEN deleted_at IS NULL THEN '' ELSE ' (soft-deleted)' END AS k, count(*) AS n
          FROM booking.invoices GROUP BY 1 ORDER BY 1""",
    "payments_by_state": """
        SELECT kind || ':' || CASE WHEN voided_at IS NOT NULL THEN 'voided' WHEN verified_at IS NOT NULL
                                   THEN 'verified' ELSE 'awaiting' END AS k, count(*) AS n
          FROM booking.payments GROUP BY 1 ORDER BY 1""",
    "b2c_children": f"""
        SELECT 'trip_case_version_itinerary' AS k, count(*) AS n FROM booking.trip_case_version_itinerary
         WHERE trip_case_version_id IN ({B2C_VERSIONS})
        UNION ALL SELECT 'version_item_costs', count(*) FROM booking.version_item_costs
         WHERE version_item_id IN (SELECT id FROM booking.trip_case_version_itinerary
                                    WHERE trip_case_version_id IN ({B2C_VERSIONS}))
        UNION ALL SELECT 'trip_case_legs', count(*) FROM booking.trip_case_legs WHERE trip_case_id IN ({B2C_CASES})
        UNION ALL SELECT 'trip_case_countries', count(*) FROM booking.trip_case_countries
         WHERE trip_case_id IN ({B2C_CASES})
        UNION ALL SELECT 'trip_case_checks', count(*) FROM booking.trip_case_checks WHERE trip_case_id IN ({B2C_CASES})
        UNION ALL SELECT 'trip_case_status_history', count(*) FROM booking.trip_case_status_history
         WHERE trip_case_id IN ({B2C_CASES})
        UNION ALL SELECT 'staff_assignments', count(*) FROM booking.staff_assignments
         WHERE trip_case_id IN ({B2C_CASES})
        UNION ALL SELECT 'supplier_bill_items', count(*) FROM booking.supplier_bill_items
         WHERE bill_id IN ({B2C_BILLS})
        UNION ALL SELECT 'b2b_holds_on_b2c_case', count(*) FROM booking.b2b_holds WHERE trip_case_id IN ({B2C_CASES})
        UNION ALL SELECT 'b2b_cancellations_on_b2c_case', count(*) FROM booking.b2b_cancellations
         WHERE trip_case_id IN ({B2C_CASES})""",
    "mixed_supplier_bills": f"""
        SELECT 'b2c_bill_with_non_b2c_line' AS k, count(DISTINCT it.bill_id) AS n
          FROM booking.supplier_bill_items it JOIN booking.supplier_bookings sb ON sb.id = it.supplier_booking_id
         WHERE it.bill_id IN ({B2C_BILLS}) AND sb.id NOT IN ({B2C_SB})""",
    "b2c_ops_files_by_kind": f"""
        SELECT kind || ':' || status AS k, count(*) AS n FROM booking.ops_files WHERE id IN ({B2C_FILES})
         GROUP BY 1 ORDER BY 1""",
    "notifications_b2c": f"""
        SELECT 'recipient_customer' AS k, count(*) AS n FROM booking.notifications WHERE recipient_type = 'customer'
        UNION ALL SELECT 'about_invoice_payment_customer', count(*) FROM booking.notifications
         WHERE entity_type IN ('invoice', 'payment', 'customer')
        UNION ALL SELECT 'about_b2c_case', count(*) FROM booking.notifications WHERE entity_id IN ({B2C_CASES})""",
    "audit_logs_b2c": f"""
        SELECT 'actor_customer' AS k, count(*) AS n FROM booking.audit_logs WHERE actor_type = 'customer'
        UNION ALL SELECT 'entity_' || entity_type, count(*) FROM booking.audit_logs
         WHERE entity_type IN ('customer', 'invoice', 'payment') GROUP BY entity_type
        UNION ALL SELECT 'entity_b2c_case_or_version', count(*) FROM booking.audit_logs
         WHERE entity_id IN ({B2C_CASES}) OR entity_id IN ({B2C_VERSIONS})""",
    "inbox_events_by_source": """
        SELECT source || ':' || contract || ':' || status
               || CASE WHEN redacted_at IS NULL THEN '' ELSE ' (redacted)' END AS k, count(*) AS n
          FROM booking.inbox_events GROUP BY 1 ORDER BY 1""",
    "inbox_cursor": "SELECT source || ':' || contract AS k, safe_id AS n FROM booking.inbox_cursor ORDER BY 1",
    "jobs_b2c_kinds": """
        SELECT kind || ':' || status AS k, count(*) AS n FROM booking.job
         WHERE kind = ANY($1::text[]) GROUP BY 1 ORDER BY 1""",
    "settings_b2c_keys": """
        SELECT key AS k, 1 AS n FROM booking.settings
         WHERE key IN ('deposit_due_days', 'balance_due_days_before_start', 'payment_due_soon_days',
                       'payment_overdue_reminder_days', 'invoice_issuer') ORDER BY 1""",
    "ops_transitions_invoice": """
        SELECT entity AS k, count(*) AS n FROM booking.ops_transitions WHERE entity = 'invoice' GROUP BY 1""",
    "money_case_edges": """
        SELECT 'money_edges' AS k, count(*) AS n FROM booking.trip_case_status_transitions
         WHERE from_status IN ('invoice_sent', 'payment_pending', 'paid')
            OR to_status IN ('invoice_sent', 'payment_pending', 'paid')""",
}
JOB_LAST_SQL = """
SELECT kind AS k, max(coalesce(finished_at, updated_at, created_at)) AS t FROM booking.job
 WHERE kind = ANY($1::text[]) GROUP BY 1 ORDER BY 1"""

STAT_SQL = """
SELECT s.n_tup_ins AS ins, s.n_tup_upd AS upd, s.n_tup_del AS del, s.seq_scan AS seq,
       coalesce(s.idx_scan, 0) AS idx, s.n_live_tup AS live, pg_total_relation_size(s.relid) AS bytes
  FROM pg_stat_user_tables s WHERE s.relid = to_regclass($1)"""
STATS_RESET_SQL = "SELECT stats_reset FROM pg_stat_database WHERE datname = current_database()"

DEPENDENTS_SQL = """
WITH t AS (SELECT to_regclass(x) AS oid FROM unnest($1::text[]) x)
SELECT DISTINCT 'view' AS kind, t.oid::regclass::text AS object, v.oid::regclass::text AS dependent
  FROM t JOIN pg_depend d ON d.refobjid = t.oid AND d.classid = 'pg_rewrite'::regclass
  JOIN pg_rewrite r ON r.oid = d.objid JOIN pg_class v ON v.oid = r.ev_class
 WHERE t.oid IS NOT NULL AND v.oid <> t.oid AND t.oid <> 'booking.trip_cases'::regclass
UNION
SELECT DISTINCT 'view_on_column', 'booking.trip_cases.customer_id', v.oid::regclass::text
  FROM pg_depend d JOIN pg_rewrite r ON r.oid = d.objid AND d.classid = 'pg_rewrite'::regclass
  JOIN pg_class v ON v.oid = r.ev_class
  JOIN pg_attribute a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid
 WHERE d.refobjid = 'booking.trip_cases'::regclass AND a.attname = 'customer_id'
UNION
SELECT 'fk_in', c.confrelid::regclass::text, c.conrelid::regclass::text || ' (' || c.conname || ')'
  FROM t JOIN pg_constraint c ON c.contype = 'f' AND c.confrelid = t.oid
 WHERE c.conrelid <> c.confrelid
UNION
SELECT 'grant', t.oid::regclass::text, pg_get_userbyid(a.grantee) || ' ' || a.privilege_type
  FROM t JOIN pg_class c ON c.oid = t.oid, aclexplode(c.relacl) a
 WHERE a.grantee <> c.relowner AND pg_get_userbyid(a.grantee) <> 'booking'
 ORDER BY 1, 2, 3"""
FUNCTIONS_SQL = r"""
SELECT p.oid::regprocedure::text AS fn FROM pg_proc p
 WHERE p.pronamespace = 'booking'::regnamespace
   AND p.prosrc ~ ('\m(booking\.)?(customers|customer_identities|customer_otps|customer_sessions|media_files|'
                   'invoices|invoice_items|payment_schedule|payments|v_invoice_balance|v_payment_schedule_status|'
                   'v_customer_summary)\M')
 ORDER BY 1"""
LATEST_SQL = "SELECT max(version) FROM booking.v_schema_versions WHERE version LIKE 'bk-%'"


def _out(section: str, value: Any) -> None:
    print(f"DRYRUN {section} {json.dumps(value, default=str, sort_keys=True)}")


async def _readable(conn: Any, rel: str) -> bool:
    return bool(
        await conn.fetchval(
            "SELECT to_regclass($1) IS NOT NULL AND has_table_privilege(to_regclass($1), 'SELECT')", rel
        )
    )


async def measure(conn: Any) -> dict[str, Any]:
    """Every section of the report (read-only), in one transaction whose search_path is pg_catalog only, so every
    regclass / regprocedure prints schema-qualified whatever the login's search_path is (role booking has
    `booking` on it)."""
    async with conn.transaction():
        await conn.execute("SET LOCAL search_path = pg_catalog")
        for statement in HASH_SETTINGS:
            await conn.execute(statement)
        return await _measure(conn)


async def _measure(conn: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "now": (await conn.fetchval("SELECT now()")).isoformat(),
        "login": await conn.fetchval("SELECT current_user"),
        "stats_reset": await conn.fetchval(STATS_RESET_SQL),
    }
    needed = (
        *DROP_TABLES,
        "booking.trip_cases",
        "booking.trip_case_versions",
        "booking.supplier_bookings",
        "booking.supplier_bills",
        "booking.supplier_bill_items",
        "booking.supplier_payouts",
        "booking.ops_files",
        "booking.inbox_events",
        "booking.notifications",
        "booking.audit_logs",
        "booking.job",
    )
    access = {rel: await _readable(conn, rel) for rel in needed}
    report["access"] = access
    if not all(access.values()):
        report["refused"] = sorted(rel for rel, ok in access.items() if not ok)
        return report
    report["latest_booking_migration"] = (
        await conn.fetchval(LATEST_SQL) if await _readable(conn, "booking.v_schema_versions") else None
    )
    pins: dict[str, dict[str, Any]] = {}
    for table in PIN_TABLES:
        row = await conn.fetchrow(PIN_SQL[table])
        pins[table] = {"n": int(row["n"]), "md5": row["h"]}
    report["pins"] = pins
    report["kept"] = {k: int(await conn.fetchval(sql)) for k, sql in KEPT.items()}
    last = await conn.fetchval(LAST_WRITE_SQL)
    report["last_b2c_write"] = last
    report["guard_30d_passes"] = last is None or last < datetime.now(UTC) - timedelta(days=GUARD_DAYS)
    breakdowns: dict[str, Any] = {}
    for name, sql in BREAKDOWNS.items():
        if name == "jobs_b2c_kinds":
            rows = await conn.fetch(sql, [*B2C_JOB_KINDS, *MIXED_JOB_KINDS])
        else:
            rows = await conn.fetch(sql)
        breakdowns[name] = {r["k"]: int(r["n"]) for r in rows}
    report["breakdowns"] = breakdowns
    report["job_last_run"] = {
        r["k"]: r["t"] for r in await conn.fetch(JOB_LAST_SQL, [*B2C_JOB_KINDS, *MIXED_JOB_KINDS])
    }
    report["b2c_jobs_busy"] = int(
        await conn.fetchval(
            "SELECT count(*) FROM booking.job WHERE kind = ANY($1::text[]) AND status IN ('queued', 'running')",
            list(B2C_JOB_KINDS),
        )
    )
    stats: dict[str, Any] = {}
    for rel in (*DROP_TABLES, "booking.trip_cases", "booking.inbox_events"):
        row = await conn.fetchrow(STAT_SQL, rel)
        stats[rel] = dict(row) if row else None
    report["pg_stat"] = stats
    report["dependents"] = [
        dict(r) for r in await conn.fetch(DEPENDENTS_SQL, [*DROP_TABLES, *DROP_VIEWS, "booking.trip_cases"])
    ]
    report["functions_naming_b2c"] = [r["fn"] for r in await conn.fetch(FUNCTIONS_SQL)]
    report["sequences_present"] = [
        s for s in DROP_SEQUENCES if await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", s)
    ]
    return report


def verdict(report: dict[str, Any]) -> dict[str, Any]:
    """What the draft migration's guards would say today (it re-checks everything at apply)."""
    reasons = []
    if report.get("refused"):
        reasons.append("no SELECT on " + ",".join(report["refused"]))
        return {"ready": False, "reasons": reasons, "notes": []}
    if not report["guard_30d_passes"]:
        reasons.append(f"a B2C row was written at {report['last_b2c_write']} (< {GUARD_DAYS} days)")
    if report["b2c_jobs_busy"]:
        reasons.append(f"{report['b2c_jobs_busy']} intake job(s) queued or running")
    known = {*DROP_TABLES, *DROP_VIEWS, *REWRITTEN_VIEWS, "booking.v_ops_funnel_daily"}
    for dep in report["dependents"]:
        if dep["kind"] in ("view", "view_on_column") and dep["dependent"] not in known:
            reasons.append(f"view {dep['dependent']} depends on {dep['object']}")
        if dep["kind"] == "fk_in" and dep["object"] != "booking.trip_cases":
            source = dep["dependent"].split(" ")[0]
            if source not in known and source != "booking.trip_cases":
                reasons.append(f"foreign key {dep['dependent']} points at {dep['object']}")
    # A grant to another app's role is not a blocker by itself (the DROP removes it), but its reader must be gone
    # first: the report lists them for the readers checklist (AA-CIS-Infra isolation probe cells included).
    notes = [
        f"{dep['object']} is granted to {dep['dependent']}: confirm its reader is removed"
        for dep in report["dependents"]
        if dep["kind"] == "grant" and dep["object"] in (*DROP_TABLES, *DROP_VIEWS)
    ]
    return {"ready": not reasons, "reasons": reasons, "notes": notes}


async def main(conn: Any) -> None:
    """DB Migrate run-script entry point (and the ECS exec path below)."""
    report = await measure(conn)
    report["verdict"] = verdict(report)
    for key in (
        "login",
        "stats_reset",
        "latest_booking_migration",
        "last_b2c_write",
        "guard_30d_passes",
        "b2c_jobs_busy",
        "kept",
        "verdict",
    ):
        _out(key, report.get(key))
    for name, value in report.get("breakdowns", {}).items():
        _out(name, value)
    print("DRYRUN_PINS " + json.dumps(report.get("pins", {}), sort_keys=True))
    print("DRYRUN_JSON " + json.dumps(report, default=str, sort_keys=True))


async def _cli() -> None:
    import asyncpg

    ssl: Any = False if os.environ.get("DB_SSL") == "disable" else "require"
    conn = await asyncpg.connect(os.environ["DATABASE_URL"], ssl=ssl)
    try:
        await conn.execute("SET default_transaction_read_only = on")
        await main(conn)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(_cli())
