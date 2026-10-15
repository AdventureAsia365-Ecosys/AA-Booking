-- CI-only stub of the CIS catalog read model Booking reads (shape: AA-Ecosys docs/architecture/db-schema-reference.md
-- and AA-CIS-App api/migrations/225_catalog_views.sql, only the columns Booking selects) plus the grant CIS 225 gives
-- role booking. catalog.v_tour / v_tour_day / v_tour_photo (AA-679 trip plans) are plain tables standing in for the
-- views; catalog.v_destination (AA-822 supplier place picker) is a view over fixed rows. Applied after the bk-NNN
-- migrations (the grant needs role booking).
CREATE SCHEMA IF NOT EXISTS catalog;

CREATE TABLE IF NOT EXISTS catalog.v_tour (
    source_system     TEXT NOT NULL DEFAULT 'cis',
    source_id         TEXT NOT NULL,
    tour_id           UUID NOT NULL DEFAULT gen_random_uuid(),
    source_version    SMALLINT,
    sku               TEXT,
    slug              TEXT,
    title             VARCHAR,
    subtitle          VARCHAR,
    summary           TEXT,
    description       TEXT,
    highlights        JSONB,
    country           TEXT,
    duration_raw      TEXT,
    duration_days     INTEGER,
    activities        JSONB,
    inclusions        TEXT,
    exclusions        TEXT,
    best_time_to_go   VARCHAR,
    group_size        TEXT,
    lifecycle_stage   TEXT,
    itinerary_text    TEXT,
    cover_photo_path  TEXT,
    photo_count       BIGINT NOT NULL DEFAULT 0,
    published_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    content_hash      TEXT
);

CREATE TABLE IF NOT EXISTS catalog.v_tour_day (
    source_system  TEXT NOT NULL DEFAULT 'cis',
    source_id      TEXT NOT NULL,
    tour_id        UUID,
    day_number     INTEGER NOT NULL,
    title          TEXT,
    body           TEXT,
    places         TEXT[]
);

CREATE TABLE IF NOT EXISTS catalog.v_tour_photo (
    source_system   TEXT NOT NULL DEFAULT 'cis',
    source_id       TEXT NOT NULL,
    tour_id         UUID,
    photo_id        UUID NOT NULL DEFAULT gen_random_uuid(),
    path            TEXT,
    place_label     TEXT,
    destination_id  UUID,
    width           INTEGER,
    height          INTEGER,
    position        BIGINT,
    updated_at      TIMESTAMPTZ
);

CREATE OR REPLACE VIEW catalog.v_destination AS
SELECT 'cis'::text AS source_system, d.id::text AS source_id, d.id AS destination_id, d.name, d.country,
       d.lat, d.lng, NULL::text AS cover_photo_path, NULL::text AS cover_image_url, 0::bigint AS photo_count,
       now() AS created_at, now() AS updated_at
FROM (VALUES
    ('d0000000-0000-4000-8000-000000000001'::uuid, 'Hanoi', 'Vietnam', 21.03::float8, 105.85::float8),
    ('d0000000-0000-4000-8000-000000000002'::uuid, 'Luang Prabang', 'Laos', 19.89::float8, 102.13::float8),
    ('d0000000-0000-4000-8000-000000000003'::uuid, 'Hoi An', 'Vietnam', 15.88::float8, 108.33::float8),
    ('d0000000-0000-4000-8000-000000000004'::uuid, 'Atlantis', 'Nowhere', 0::float8, 0::float8)
) AS d (id, name, country, lat, lng);

-- CIS 225 grants (catalog_owner owns the views; role booking reads them).
GRANT USAGE ON SCHEMA catalog TO booking;
GRANT SELECT ON catalog.v_tour, catalog.v_tour_day, catalog.v_tour_photo, catalog.v_destination TO booking;
