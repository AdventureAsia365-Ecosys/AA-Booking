-- CI-only stub of the TripPlanner objects the inquiry consumer reads (shape: TripPlanner tp-008
-- migrations/008_inquiry_outbox.sql) plus the grant TripPlanner tp-009 gives role booking.
-- Applied after the bk-NNN migrations, since the grant needs role booking.
CREATE SCHEMA IF NOT EXISTS tripplanner;

CREATE TABLE IF NOT EXISTS tripplanner.outbox (
    id              BIGSERIAL PRIMARY KEY,
    event           TEXT NOT NULL,
    schema_version  INT  NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    aggregate_id    UUID NOT NULL,
    payload         JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT outbox_event_chk CHECK (event IN ('inquiry.submitted'))
);

CREATE OR REPLACE VIEW tripplanner.v_inquiry_outbox AS
SELECT id, schema_version, idempotency_key, aggregate_id AS trip_id, payload, created_at
FROM tripplanner.outbox
WHERE event = 'inquiry.submitted';

-- tp-009 (AA-TripPlanner-Web migrations/009_booking_inquiry_grant.sql)
GRANT USAGE ON SCHEMA tripplanner TO booking;
GRANT SELECT ON tripplanner.v_inquiry_outbox TO booking;
