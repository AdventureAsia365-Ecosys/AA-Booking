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

-- Mirror of CIS 462 (AA-1155, root ADR 0006a A14, contract staff.roles.v1): the key-only view Booking reads for a
-- booking-custom holder and the owner markers. On Dev it reads shared.admin_users and shared.staff_app_grant; here a
-- stub table stands in, filled by the tests as aa_cis_admin. Columns and grant as db-schema-reference.md: SELECT (and
-- USAGE on schema shared) to booking only; the stub table itself stays closed to booking.
CREATE TABLE IF NOT EXISTS shared.staff_booking_grant_stub (
    cognito_sub        text     PRIMARY KEY,
    booking_role_code  text,
    is_owner           boolean  NOT NULL DEFAULT false,
    is_business_owner  boolean  NOT NULL DEFAULT false
);
CREATE OR REPLACE VIEW shared.v_staff_booking_grant WITH (security_barrier = true) AS
SELECT cognito_sub, booking_role_code, is_owner, is_business_owner FROM shared.staff_booking_grant_stub;
REVOKE ALL ON shared.v_staff_booking_grant, shared.staff_booking_grant_stub FROM PUBLIC;
GRANT USAGE ON SCHEMA shared TO booking;
GRANT SELECT ON shared.v_staff_booking_grant TO booking;
