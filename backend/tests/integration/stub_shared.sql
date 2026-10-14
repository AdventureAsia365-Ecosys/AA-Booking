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
