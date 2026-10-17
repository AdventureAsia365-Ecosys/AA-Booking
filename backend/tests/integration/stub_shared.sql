-- CI-only stub of the CIS-owned objects bk-NNN migrations touch on the shared RDS.
-- Shape mirrors AA-Ecosys docs/architecture/db-schema-reference.md (shared.schema_versions).
CREATE SCHEMA IF NOT EXISTS shared;

CREATE TABLE IF NOT EXISTS shared.schema_versions (
    version      VARCHAR PRIMARY KEY,
    description  TEXT,
    applied_at   TIMESTAMPTZ NOT NULL
);

-- A CIS row, so tests can prove role booking never sees non-Booking versions.
INSERT INTO shared.schema_versions (version, description, applied_at)
VALUES ('222', 'CIS stub row', now())
ON CONFLICT (version) DO NOTHING;

-- Mirror of CIS 243 (X1 plan S4): what bk_owner needs from CIS once DB Migrate runs booking files as bk_owner
-- (registration INSERT, the runner's SELECT) and once booking.v_schema_versions is owned by bk_owner (a view
-- reads its base table with its owner's rights). bk_owner comes from tests/integration/bootstrap_roles.py.
GRANT USAGE ON SCHEMA shared TO bk_owner;
GRANT SELECT, INSERT ON shared.schema_versions TO bk_owner;
