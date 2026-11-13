-- Migration bk-057: AA-1093 lane L3 — the Booking half of the tenant offboarding erasure (root plan
-- docs/audits/2026-11-13-S279-session/aa1093-plan.md Part 2; tech lead decisions D3 and D8, S279): the erasure log, the
-- report view CIS reads, and the two narrow write paths the anonymisation needs on append-only tables.
--
-- Owner: AA-Booking (key bk-NNN). AA-CIS-Infra Migration Sync copies it byte for byte to
-- db/migrations/booking/057_tenant_erasure_log.sql; on Dev it is applied as bk_owner (the live owner of schema booking
-- since bk-014) by Infra Migration Apply once every job table is idle (root ADR 0017a). Never run it from a laptop.
--
-- Depends on bk-001 (audit_logs, set_updated_at), bk-003 (trip_case_status_history), bk-014 (bk_owner) and bk-017
-- (USAGE on schema booking for cis_app). bk-058 is the next free key (the B2C table drop takes it).
--
-- What:
--   1. booking.tenant_erasure_log: one row per erasure request and mode (dry_run | apply; UNIQUE (request_id, mode)).
--      plan_hash (SHA-256 hex of the per-table plan: action, count and the md5 fingerprint of id + updated time of the
--      rows the step changes), counts ({step: rows}), status (dry_run: planned | stale; apply: applied | refused |
--      failed), detail (a reason code such as plan_changed, never free text), source (cis = a request of
--      catalog.v_tenant_erasure_v1, cli = python -m app.b2b.tenant_erase), the job id, times. Ids, counts and hashes
--      only: no name, no note, no row value. Role booking: SELECT, INSERT, UPDATE (no DELETE: the log is the record
--      that the agency was erased, and bk_tenant_pull reads it to never write the agency name back).
--      Row level security ENABLE + FORCE like booking.b2b_partners (bk-049 / bk-053; the tenant_id rule of the RLS
--      catalogue guard): the back office (no app.tenant_id bound) reads and writes every row; a session that binds
--      app.tenant_id reads its own rows only and writes none.
--   2. booking.v_tenant_erasure_report_v1 (contract tenant.erasure.v1, the Booking report): request_id, tenant_id,
--      mode, plan_hash, counts, status, detail, created_at, updated_at, finished_at. SELECT to booking and to the CIS
--      runtime role cis_app (the tenant_erasure_saga reads each app's plan hash and its apply result; AA-CIS-Infra
--      db/owners.yaml booking.consumer_grants + the isolation probe cell consumer.contract_grants.bk-057).
--   3. booking.audit_logs stays append-only for everything but a tenant erasure: role booking gets UPDATE on the
--      columns before, after and entity_label only, and the BEFORE UPDATE trigger audit_logs_erase_guard refuses any
--      update unless the transaction set booking.tenant_erase (app.b2b.tenant_erase sets it to the request id, SET
--      LOCAL), and then lets through only the redaction: before / after become NULL or {"redacted":"tenant_erased"},
--      entity_label NULL or unchanged; every other column unchanged.
--   4. booking.trip_case_status_history (a status note can carry a cancel reason): role booking gets UPDATE on note
--      only, and trip_case_status_history_erase_guard lets through only note -> NULL inside a tenant erasure.
--
-- Class: contract (root ADR 0017 d3): new triggers and column grants on existing tables and a cross-owner grant. On Dev
-- Migration Apply applies it when the job tables are idle (ADR 0017a); the code PR raises readiness to bk-057 (MIG-4).
-- DM-6.1: no personal data in the new table (ids, counts, hashes, reason codes). DM-7.1: one new table, its retention
-- row is in the root data standards (02-data-management.md, booking.tenant_erasure_log).
-- Locks: CREATE TRIGGER takes SHARE ROW EXCLUSIVE on audit_logs and trip_case_status_history (hundreds of rows, an
-- instant); lock_timeout 5s.
-- Re-runnable (the Integration Tests replay every file): IF NOT EXISTS, CREATE OR REPLACE, DROP ... IF EXISTS,
-- repeatable grants.
-- Undo (a later bk- file, after the code and the CIS reader are reverted): DROP VIEW booking.v_tenant_erasure_report_v1;
-- DROP the two erase guard triggers and functions; REVOKE UPDATE (before, after, entity_label) ON booking.audit_logs
-- and UPDATE (note) ON booking.trip_case_status_history FROM booking; the log table stays (the erasure record).

BEGIN;

SET LOCAL lock_timeout = '5s';

DO $$
BEGIN
    IF current_user <> 'bk_owner' THEN
        RAISE EXCEPTION 'bk-057: expected to run as bk_owner (live owner of schema booking), running as %', current_user;
    END IF;
    IF to_regclass('booking.audit_logs') IS NULL OR to_regclass('booking.trip_case_status_history') IS NULL THEN
        RAISE EXCEPTION 'bk-057: needs bk-001 (audit_logs) and bk-003 (trip_case_status_history) first';
    END IF;
    IF EXISTS (SELECT 1 FROM shared.schema_versions
                WHERE version = 'bk-057' AND description NOT LIKE 'AA-1093 tenant erasure:%') THEN
        RAISE EXCEPTION 'bk-057: the key bk-057 is already held by another migration (not AA-1093): renumber one';
    END IF;
END $$;

-- ── 1. The erasure log ───────────────────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS booking.tenant_erasure_log (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id  UUID        NOT NULL,
    tenant_id   UUID        NOT NULL,
    mode        TEXT        NOT NULL,
    plan_hash   TEXT        NOT NULL,
    counts      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    status      TEXT        NOT NULL,
    detail      TEXT,
    source      TEXT        NOT NULL,
    job_id      UUID,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    CONSTRAINT tenant_erasure_log_request_mode_uq UNIQUE (request_id, mode),
    CONSTRAINT tenant_erasure_log_mode_chk CHECK (mode IN ('dry_run', 'apply')),
    CONSTRAINT tenant_erasure_log_status_chk CHECK (
        (mode = 'dry_run' AND status IN ('planned', 'stale'))
        OR (mode = 'apply' AND status IN ('applied', 'refused', 'failed'))),
    CONSTRAINT tenant_erasure_log_hash_chk CHECK (plan_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT tenant_erasure_log_counts_chk CHECK (jsonb_typeof(counts) = 'object'),
    CONSTRAINT tenant_erasure_log_detail_chk CHECK (detail IS NULL OR detail ~ '^[a-z][a-z0-9_]{0,63}$'),
    CONSTRAINT tenant_erasure_log_source_chk CHECK (source IN ('cis', 'cli')),
    CONSTRAINT tenant_erasure_log_finished_chk CHECK (finished_at IS NULL OR mode = 'apply')
);

CREATE INDEX IF NOT EXISTS tenant_erasure_log_tenant_idx
    ON booking.tenant_erasure_log (tenant_id) WHERE mode = 'apply' AND status = 'applied';

COMMENT ON TABLE booking.tenant_erasure_log IS
    'AA-1093 (bk-057, contract tenant.erasure.v1): one row per tenant erasure request and mode (dry_run | apply), '
    'written by app.b2b.tenant_erase (job bk_tenant_erase, or the CLI). Ids, counts and hashes only. An applied row '
    'makes bk_tenant_pull skip the agency (the erased name is never written back). Never deleted by role booking.';
COMMENT ON COLUMN booking.tenant_erasure_log.request_id IS
    'The CIS erasure request (catalog.v_tenant_erasure_v1), or a UUIDv5 of the tenant for a CLI run.';
COMMENT ON COLUMN booking.tenant_erasure_log.plan_hash IS
    'SHA-256 hex of the plan: per step its action, row count and md5(string_agg(id || updated time)), no row value.';
COMMENT ON COLUMN booking.tenant_erasure_log.counts IS 'Rows per step ({step: count}); kept rows under "kept".';
COMMENT ON COLUMN booking.tenant_erasure_log.detail IS
    'A reason code (plan_changed, pilot_tenant, not_synthetic, ...), never free text.';

DROP TRIGGER IF EXISTS tenant_erasure_log_set_updated_at ON booking.tenant_erasure_log;
CREATE TRIGGER tenant_erasure_log_set_updated_at
    BEFORE UPDATE ON booking.tenant_erasure_log FOR EACH ROW EXECUTE FUNCTION booking.set_updated_at();

ALTER TABLE booking.tenant_erasure_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE booking.tenant_erasure_log FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_erasure_log_back_office ON booking.tenant_erasure_log;
CREATE POLICY tenant_erasure_log_back_office ON booking.tenant_erasure_log
    AS PERMISSIVE FOR ALL TO booking, bk_owner
    USING (NULLIF(current_setting('app.tenant_id', true), '') IS NULL)
    WITH CHECK (NULLIF(current_setting('app.tenant_id', true), '') IS NULL);

DROP POLICY IF EXISTS tenant_erasure_log_tenant ON booking.tenant_erasure_log;
CREATE POLICY tenant_erasure_log_tenant ON booking.tenant_erasure_log
    AS PERMISSIVE FOR SELECT TO booking, bk_owner
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

COMMENT ON POLICY tenant_erasure_log_back_office ON booking.tenant_erasure_log IS
    'bk-057 (AA-1093): the back office (jobs, the CLI, the report view read by cis_app) with no tenant bound: every row.';
COMMENT ON POLICY tenant_erasure_log_tenant ON booking.tenant_erasure_log IS
    'bk-057 (AA-1093): a session that binds app.tenant_id reads that tenant''s rows only and writes none.';

REVOKE DELETE, TRUNCATE ON booking.tenant_erasure_log FROM booking;
GRANT SELECT, INSERT, UPDATE ON booking.tenant_erasure_log TO booking;

-- ── 2. The report view CIS reads ─────────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW booking.v_tenant_erasure_report_v1 AS
SELECT request_id, tenant_id, mode, plan_hash, counts, status, detail, created_at, updated_at, finished_at
  FROM booking.tenant_erasure_log;

COMMENT ON VIEW booking.v_tenant_erasure_report_v1 IS
    'AA-1093 (bk-057, contract tenant.erasure.v1): the Booking erasure report for the CIS tenant_erasure_saga: per '
    'request and mode the plan hash, the row counts per step, the status and a reason code. Ids, counts and hashes '
    'only. Columns are only appended.';

REVOKE INSERT, UPDATE, DELETE ON booking.v_tenant_erasure_report_v1 FROM booking;
GRANT SELECT ON booking.v_tenant_erasure_report_v1 TO booking;
GRANT USAGE ON SCHEMA booking TO cis_app;
GRANT SELECT ON booking.v_tenant_erasure_report_v1 TO cis_app;

-- ── 3. audit_logs: append-only except the erasure redaction ──────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION booking.audit_logs_erase_guard() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog
AS $$
DECLARE
    v_marker CONSTANT jsonb := '{"redacted": "tenant_erased"}'::jsonb;
BEGIN
    IF NULLIF(current_setting('booking.tenant_erase', true), '') IS NULL THEN
        RAISE EXCEPTION 'booking.audit_logs is append-only (bk-001): only a tenant erasure redacts a row (bk-057)'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF (NEW.id, NEW.actor_type, NEW.actor_id, NEW.actor_ref, NEW.action, NEW.entity_type, NEW.entity_id,
        NEW.created_at, NEW.summary)
       IS DISTINCT FROM (OLD.id, OLD.actor_type, OLD.actor_id, OLD.actor_ref, OLD.action, OLD.entity_type,
                         OLD.entity_id, OLD.created_at, OLD.summary) THEN
        RAISE EXCEPTION 'audit row %: a tenant erasure redacts before / after / entity_label only', OLD.id
            USING ERRCODE = 'check_violation';
    END IF;
    IF (NEW.before IS NOT NULL AND NEW.before <> v_marker) OR (NEW.after IS NOT NULL AND NEW.after <> v_marker)
       OR (NEW.entity_label IS NOT NULL AND NEW.entity_label IS DISTINCT FROM OLD.entity_label) THEN
        RAISE EXCEPTION 'audit row %: a tenant erasure writes only NULL or the redaction marker', OLD.id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;

COMMENT ON FUNCTION booking.audit_logs_erase_guard() IS
    'bk-057 (AA-1093): audit_logs stays append-only; inside a tenant erasure (booking.tenant_erase set) an update may '
    'only set before / after to NULL or {"redacted":"tenant_erased"} and entity_label to NULL.';

DROP TRIGGER IF EXISTS audit_logs_erase_guard ON booking.audit_logs;
CREATE TRIGGER audit_logs_erase_guard
    BEFORE UPDATE ON booking.audit_logs FOR EACH ROW EXECUTE FUNCTION booking.audit_logs_erase_guard();

GRANT UPDATE (before, after, entity_label) ON booking.audit_logs TO booking;

-- ── 4. trip_case_status_history: the note is cleared inside a tenant erasure ─────────────────────────────────────
CREATE OR REPLACE FUNCTION booking.trip_case_status_history_erase_guard() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog
AS $$
BEGIN
    IF NULLIF(current_setting('booking.tenant_erase', true), '') IS NULL THEN
        RAISE EXCEPTION 'booking.trip_case_status_history is append-only: only a tenant erasure clears a note (bk-057)'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.note IS NOT NULL
       OR (NEW.id, NEW.trip_case_id, NEW.from_status, NEW.to_status, NEW.changed_by, NEW.changed_at, NEW.seq)
          IS DISTINCT FROM (OLD.id, OLD.trip_case_id, OLD.from_status, OLD.to_status, OLD.changed_by, OLD.changed_at,
                            OLD.seq) THEN
        RAISE EXCEPTION 'status history row %: a tenant erasure only clears the note', OLD.id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;

COMMENT ON FUNCTION booking.trip_case_status_history_erase_guard() IS
    'bk-057 (AA-1093): the status history stays append-only; inside a tenant erasure (booking.tenant_erase set) an '
    'update may only set note to NULL.';

DROP TRIGGER IF EXISTS trip_case_status_history_erase_guard ON booking.trip_case_status_history;
CREATE TRIGGER trip_case_status_history_erase_guard
    BEFORE UPDATE ON booking.trip_case_status_history FOR EACH ROW
    EXECUTE FUNCTION booking.trip_case_status_history_erase_guard();

GRANT UPDATE (note) ON booking.trip_case_status_history TO booking;

-- ── Post-check ───────────────────────────────────────────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF NOT (SELECT relrowsecurity AND relforcerowsecurity FROM pg_class
             WHERE oid = 'booking.tenant_erasure_log'::regclass) THEN
        RAISE EXCEPTION 'bk-057: row level security is not enabled and forced on booking.tenant_erasure_log';
    END IF;
    IF has_table_privilege('booking', 'booking.tenant_erasure_log', 'DELETE')
       OR has_table_privilege('cis_app', 'booking.tenant_erasure_log', 'SELECT') THEN
        RAISE EXCEPTION 'bk-057: role booking can delete erasure rows, or cis_app can read the table';
    END IF;
    IF NOT has_table_privilege('cis_app', 'booking.v_tenant_erasure_report_v1', 'SELECT') THEN
        RAISE EXCEPTION 'bk-057: cis_app cannot read booking.v_tenant_erasure_report_v1';
    END IF;
    IF has_table_privilege('booking', 'booking.v_tenant_erasure_report_v1', 'INSERT') THEN
        RAISE EXCEPTION 'bk-057: booking.v_tenant_erasure_report_v1 is writable';
    END IF;
    IF has_table_privilege('booking', 'booking.audit_logs', 'UPDATE')
       OR has_column_privilege('booking', 'booking.audit_logs', 'action', 'UPDATE')
       OR has_table_privilege('booking', 'booking.trip_case_status_history', 'UPDATE')
       OR has_column_privilege('booking', 'booking.trip_case_status_history', 'to_status', 'UPDATE') THEN
        RAISE EXCEPTION 'bk-057: role booking can update more than the erasure columns of an append-only table';
    END IF;
END $$;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('bk-057', now(), 'AA-1093 tenant erasure: booking.tenant_erasure_log (RLS), booking.v_tenant_erasure_report_v1 (CIS reader cis_app), erase guards on audit_logs and trip_case_status_history')
ON CONFLICT (version) DO NOTHING;

COMMIT;
