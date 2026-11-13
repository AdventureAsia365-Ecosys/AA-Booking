-- Migration bk-056: S279 thresholds program, lane L3 — Booking's threshold and policy values move onto Pricing > Policy
-- (root ADR 0005b decision 15; S279 thresholds audit, docs/audits/2026-11-13-S279-session/thresholds-audit/ in the
-- root repo, sections 3.2, 3.3 and 5.1 "Booking").
--
-- Owner: AA-Booking (key bk-NNN). AA-CIS-Infra Migration Sync copies it byte for byte to
-- db/migrations/booking/056_s279_policy_settings_groups.sql and Migration Apply applies it on Dev as bk_owner once
-- every job table is idle (root ADR 0017a). Never run it from a laptop.
--
-- Why: the tech lead ruled in S279 that every threshold, limit, cap, retention window and policy value of every app is
-- editable on an admin page (administrator role, seeded default, audited). Booking gets no new settings table: its
-- page is Pricing > Policy (booking.pricing_policy, bk-050: versioned, maker-checker, an event per step, an audit row
-- per write). Until now the values below were rows of booking.settings (role booking has SELECT only, so a change
-- needed a migration) or constants in code.
--
-- What:
--   1. Version 1 of 19 new parameters (keys, types and ranges: backend/app/pricing/policy_params.py), made by the
--      migration ('seed:bk-056'), submitted, and approved by 'tech lead S279' (all audit defaults approved), in force
--      from 2026-01-01 like the bk-050 seed. Each seed is TODAY'S EFFECTIVE VALUE: a value moved from booking.settings
--      is copied from its row when the row holds a whole number inside the range the code accepted (the code fell
--      back to its default otherwise, so the default is what was in force); a value that was a code constant is that
--      constant. Groups:
--        holds        b2b_hold_hours (48), b2b_hold_extend_hours (72), b2b_hold_max_extensions (1, the
--                     b2b_holds_extended_chk CHECK caps it at 1), b2b_rehold_limit_per_day (3)
--        B2B desk     b2b_on_request_answer_business_days (1), b2b_on_request_due_soon_hours (4)
--        travellers   b2b_child_age_max (11), b2b_infant_age_max (1)
--        departures   departure_decision_days_default (21)
--        operations   pre_departure_days (14), supplier_answer_days (2), trip_check_reminder_days (3),
--                     supplier_bill_due_soon_days (3)
--        agency bills agency_reminder_steps_days ([1, 7, 14]; there never was a settings row)
--        rounding     rounding_increment_by_currency (5 per selling currency)
--        retention    retention_job_succeeded_days (30), retention_job_failed_days (90),
--                     retention_inbox_redact_days (90), emergency_contact_keep_days (90)
--   2. booking.settings: the moved rows say "Moved (bk-056)"; the dead B2C rows (customer deposit, proposal validity,
--      the customer invoice schedule and its reminders, issuer and payment text) and ops_docs_max_bytes (never read;
--      the upload cap is the code bound tied to the staff BFF body cap) say "Retired (bk-056)". No row is deleted
--      (a DELETE needs the destructive class): the code of this lane stops reading them, a later destructive file
--      may drop them. b2b_ops_timezone and b2b_ops_contact stay (a time zone and a printed text line, not policy
--      values).
--
-- Class: contract (root ADR 0017 d3): config rows into existing tables and UPDATEs of existing rows. Migration-only
-- PR first (MIG-4), then the code PR raises readiness to bk-056. On Dev: Infra Migration Apply once every job table
-- is idle (root ADR 0017a). One DO block (the post-check), no function or trigger.
-- The running code before the code PR still reads booking.settings: nothing it reads changes value here.
-- DM-6.1: no personal data (policy values, conf:commercial). DM-7.1: no new table.
-- Locks: row locks on booking.settings (13 rows) only.
-- Re-runnable (the Integration Tests replay every file): ON CONFLICT DO NOTHING; the UPDATEs skip rows already marked.
-- Undo (a later bk- file, after the code is reverted): a new version per key is never needed for an undo (the old
-- code reads booking.settings again); DELETE the seed:bk-056 events and versions if they must go.

BEGIN;

SET LOCAL lock_timeout = '5s';

-- ── 1. The new parameters, version 1 = today's effective value ───────────────────────────────────────────────────

-- d.setting: the booking.settings row the value moves from (NULL = it was a code constant); d.low / d.high: the range
-- the code accepted for that row (outside it the code used d.fallback).
INSERT INTO booking.pricing_policy (id, param_key, version_no, value, unit, effective_from, reason, created_by)
SELECT d.id::uuid, d.param_key, 1,
       CASE WHEN jsonb_typeof(s.value) = 'number' THEN
            CASE WHEN (s.value::text)::numeric = trunc((s.value::text)::numeric)
                      AND (s.value::text)::numeric BETWEEN d.low AND d.high
                 THEN to_jsonb(((s.value::text)::numeric)::integer)
                 ELSE d.fallback END
            ELSE d.fallback END,
       d.unit, DATE '2026-01-01', d.reason, 'seed:bk-056'
FROM (VALUES
    ('00000000-0000-4000-8000-0000000b5021', 'b2b_hold_hours', 'b2b_hold_hours', 1, 720, '48'::jsonb, 'hours',
     'S279 L3: moved from booking.settings b2b_hold_hours (AA-973, 48 h); today''s value.'),
    ('00000000-0000-4000-8000-0000000b5022', 'b2b_hold_extend_hours', 'b2b_hold_extend_hours', 1, 720, '72'::jsonb,
     'hours', 'S279 L3: moved from booking.settings b2b_hold_extend_hours (AA-974, +72 h); today''s value.'),
    ('00000000-0000-4000-8000-0000000b5023', 'b2b_hold_max_extensions', NULL, 0, 1, '1'::jsonb, 'extensions',
     'S279 L3: a hold is extended at most once (AA-974 code rule; the table CHECK allows 0 or 1).'),
    ('00000000-0000-4000-8000-0000000b5024', 'b2b_rehold_limit_per_day', 'b2b_rehold_limit_per_day', 0, 100,
     '3'::jsonb, 'holds', 'S279 L3: moved from booking.settings b2b_rehold_limit_per_day (AA-974); today''s value.'),
    ('00000000-0000-4000-8000-0000000b5025', 'b2b_on_request_answer_business_days',
     'b2b_on_request_answer_business_days', 1, 10, '1'::jsonb, 'business days',
     'S279 L3: moved from booking.settings (AA-975 on-request answer SLA); today''s value.'),
    ('00000000-0000-4000-8000-0000000b5026', 'b2b_on_request_due_soon_hours', NULL, 1, 24, '4'::jsonb, 'hours',
     'S279 L3: an on-request line shows as due soon 4 hours before its answer deadline (former code constant).'),
    ('00000000-0000-4000-8000-0000000b5027', 'b2b_child_age_max', 'b2b_child_age_max', 2, 17, '11'::jsonb, 'years',
     'S279 L3: moved from booking.settings b2b_child_age_max (AA-1075 bk-15); today''s value.'),
    ('00000000-0000-4000-8000-0000000b5028', 'b2b_infant_age_max', 'b2b_infant_age_max', 0, 4, '1'::jsonb, 'years',
     'S279 L3: moved from booking.settings b2b_infant_age_max (AA-1075 bk-15); today''s value.'),
    ('00000000-0000-4000-8000-0000000b5029', 'departure_decision_days_default', NULL, 0, 365, '21'::jsonb, 'days',
     'S279 L3: a new departure decides go or cancel 21 days before it starts unless staff set another day (former '
     'code constant, AA-1073).'),
    ('00000000-0000-4000-8000-0000000b5030', 'pre_departure_days', 'pre_departure_days', 0, 120, '14'::jsonb, 'days',
     'S279 L3: moved from booking.settings pre_departure_days (AA-682); today''s value.'),
    ('00000000-0000-4000-8000-0000000b5031', 'supplier_answer_days', 'supplier_answer_days', 1, 60, '2'::jsonb,
     'days', 'S279 L3: moved from booking.settings supplier_answer_days (AA-682); today''s value.'),
    ('00000000-0000-4000-8000-0000000b5032', 'trip_check_reminder_days', 'trip_check_reminder_days', 0, 60,
     '3'::jsonb, 'days', 'S279 L3: moved from booking.settings trip_check_reminder_days (AA-682 S2); today''s value.'),
    ('00000000-0000-4000-8000-0000000b5033', 'supplier_bill_due_soon_days', 'supplier_bill_due_soon_days', 0, 60,
     '3'::jsonb, 'days',
     'S279 L3: moved from booking.settings supplier_bill_due_soon_days (AA-682 S4); today''s value.'),
    ('00000000-0000-4000-8000-0000000b5034', 'emergency_contact_keep_days', 'emergency_contact_keep_days', 0, 3650,
     '90'::jsonb, 'days',
     'S279 L3: moved from booking.settings emergency_contact_keep_days (DM-7 row of trip_case_checks); today''s value.'),
    ('00000000-0000-4000-8000-0000000b5035', 'agency_reminder_steps_days', NULL, 1, 365, '[1, 7, 14]'::jsonb, 'days',
     'S279 L3: overdue agency invoices are reminded 1, 7 and 14 days after the due date (former code default, '
     'AA-1037 G4).'),
    ('00000000-0000-4000-8000-0000000b5036', 'rounding_increment_by_currency', NULL, 0, 0,
     '{"AUD": "5", "EUR": "5", "GBP": "5", "SGD": "5", "USD": "5"}'::jsonb, NULL,
     'S279 L3: customer-facing totals are rounded up to 5 units of each selling currency (ADR 0005; former code '
     'constant).'),
    ('00000000-0000-4000-8000-0000000b5037', 'retention_job_succeeded_days', NULL, 1, 365, '30'::jsonb, 'days',
     'S279 L3: succeeded jobs are archived and deleted 30 days after they finish (AA-825 window, approved E3).'),
    ('00000000-0000-4000-8000-0000000b5038', 'retention_job_failed_days', NULL, 1, 730, '90'::jsonb, 'days',
     'S279 L3: failed and cancelled jobs are archived and deleted 90 days after they finish (AA-825, approved E3).'),
    ('00000000-0000-4000-8000-0000000b5039', 'retention_inbox_redact_days', NULL, 1, 365, '90'::jsonb, 'days',
     'S279 L3: personal data in inbox payloads is redacted 90 days after receipt (AA-825, DM-6.6, approved E3).')
) AS d(id, param_key, setting, low, high, fallback, unit, reason)
LEFT JOIN booking.settings s ON s.key = d.setting
ON CONFLICT DO NOTHING;

INSERT INTO booking.pricing_policy_events (id, policy_id, maker, event, actor)
SELECT ('00000000-0000-4000-8000-0000000b6' || right(p.id::text, 3))::uuid, p.id, p.created_by, 'submitted',
       p.created_by
FROM booking.pricing_policy p
WHERE p.created_by IN ('seed:bk-056')
ON CONFLICT DO NOTHING;

INSERT INTO booking.pricing_policy_events (id, policy_id, maker, event, actor)
SELECT ('00000000-0000-4000-8000-0000000b7' || right(p.id::text, 3))::uuid, p.id, p.created_by, 'approved',
       'tech lead S279'
FROM booking.pricing_policy p
WHERE p.created_by IN ('seed:bk-056')
ON CONFLICT DO NOTHING;

-- ── 2. booking.settings: moved and retired rows (kept; this lane's code no longer reads them) ────────────────────

UPDATE booking.settings
SET description = 'Moved (bk-056): now the pricing policy parameter ' || key
                  || ' (Pricing > Policy, maker-checker); this row is no longer read.'
WHERE key IN ('b2b_hold_hours', 'b2b_hold_extend_hours', 'b2b_rehold_limit_per_day',
              'b2b_on_request_answer_business_days', 'b2b_child_age_max', 'b2b_infant_age_max',
              'pre_departure_days', 'supplier_answer_days', 'trip_check_reminder_days',
              'supplier_bill_due_soon_days', 'emergency_contact_keep_days')
  AND description NOT LIKE 'Moved (bk-056)%';

UPDATE booking.settings
SET description = 'Retired (bk-056): a B2C value (the customer channel is retired, AA-1046); this row is no longer '
                  'read by the API.'
WHERE key IN ('deposit_pct', 'quote_validity_days', 'deposit_due_days', 'balance_due_days_before_start',
              'payment_due_soon_days', 'payment_overdue_reminder_days', 'invoice_issuer', 'payment_instructions')
  AND description NOT LIKE 'Retired (bk-056)%';

UPDATE booking.settings
SET description = 'Retired (bk-056): never read; the operations document cap is the code bound MAX_BYTES of '
                  'app/ops/files.py (3 MB), tied to the staff BFF body cap.'
WHERE key IN ('ops_docs_max_bytes') AND description NOT LIKE 'Retired (bk-056)%';

-- ── 3. Post-check: every new parameter has an approved version 1 ────────────────────────────────────────────────

DO $$
DECLARE
    n integer;
BEGIN
    SELECT count(*) INTO n
    FROM booking.pricing_policy p
    JOIN booking.pricing_policy_events a ON a.policy_id = p.id AND a.event = 'approved'
    WHERE p.created_by IN ('seed:bk-056') AND p.version_no = 1;
    IF n <> 19 THEN
        RAISE EXCEPTION 'bk-056: % of the 19 new pricing policy parameters have an approved version 1', n
            USING HINT = 'A key already had a version 1 made by someone else; the seed must not overwrite it.';
    END IF;
END
$$;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('bk-056', now(), 'S279 L3: 19 threshold and policy values (holds, desk, ages, departures, operations, agency reminders, rounding, retention) as pricing policy parameters, booking.settings rows moved or retired')
ON CONFLICT (version) DO NOTHING;

COMMIT;
