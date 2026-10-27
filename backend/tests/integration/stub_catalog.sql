-- CI-only stub of the CIS catalog read model Booking reads (shape: AA-Ecosys docs/architecture/db-schema-reference.md
-- and AA-CIS-App api/migrations/225_catalog_views.sql, only the columns Booking selects) plus the grant CIS 225 gives
-- role booking. catalog.v_tour / v_tour_day / v_tour_photo (AA-679 trip plans) are plain tables standing in for the
-- views; catalog.v_destination (AA-822 supplier place picker) is a view over fixed rows; v_destination_photo and
-- v_country (AA-996 countries) are plain tables. Applied after the bk-NNN
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

CREATE TABLE IF NOT EXISTS catalog.v_destination_photo (
    destination_id  UUID,
    photo_id        UUID NOT NULL DEFAULT gen_random_uuid(),
    path            TEXT,
    place_label     TEXT,
    tour_id         UUID,
    width           INTEGER,
    height          INTEGER,
    position        BIGINT,
    updated_at      TIMESTAMPTZ
);

-- CIS 279: catalog.v_country (every ISO 3166-1 entry; a few rows here) and v_destination.country_iso2 (CIS resolves a
-- place's country text; NULL when it cannot). Pakistan is a CIS country outside booking.countries (bk-005 / bk-008).
CREATE TABLE IF NOT EXISTS catalog.v_country (
    iso2          CHAR(2) PRIMARY KEY,
    iso3          CHAR(3),
    numeric_code  CHAR(3),
    name          TEXT NOT NULL,
    iso_name      TEXT NOT NULL
);
INSERT INTO catalog.v_country (iso2, iso3, numeric_code, name, iso_name) VALUES
    ('VN', 'VNM', '704', 'Vietnam', 'Viet Nam'),
    ('LA', 'LAO', '418', 'Laos', 'Lao People''s Democratic Republic'),
    ('KH', 'KHM', '116', 'Cambodia', 'Cambodia'),
    ('TH', 'THA', '764', 'Thailand', 'Thailand'),
    ('JP', 'JPN', '392', 'Japan', 'Japan'),
    ('BT', 'BTN', '064', 'Bhutan', 'Bhutan'),
    ('PK', 'PAK', '586', 'Pakistan', 'Pakistan'),
    ('TW', 'TWN', '158', 'Taiwan', 'Taiwan, Province of China'),
    ('KG', 'KGZ', '417', 'Kyrgyzstan', 'Kyrgyzstan')
ON CONFLICT (iso2) DO NOTHING;

CREATE OR REPLACE VIEW catalog.v_destination AS
SELECT 'cis'::text AS source_system, d.id::text AS source_id, d.id AS destination_id, d.name, d.country,
       d.lat, d.lng, NULL::text AS cover_photo_path, NULL::text AS cover_image_url, 0::bigint AS photo_count,
       now() AS created_at, now() AS updated_at, d.country_iso2
FROM (VALUES
    ('d0000000-0000-4000-8000-000000000001'::uuid, 'Hanoi', 'Vietnam', 21.03::float8, 105.85::float8, 'VN'::char(2)),
    ('d0000000-0000-4000-8000-000000000002'::uuid, 'Luang Prabang', 'Laos', 19.89::float8, 102.13::float8, 'LA'),
    ('d0000000-0000-4000-8000-000000000003'::uuid, 'Hoi An', 'Vietnam', 15.88::float8, 108.33::float8, 'VN'),
    ('d0000000-0000-4000-8000-000000000004'::uuid, 'Atlantis', 'Nowhere', 0::float8, 0::float8, NULL),
    ('d0000000-0000-4000-8000-000000000005'::uuid, 'Lahore', 'Pakistan', 31.55::float8, 74.34::float8, 'PK')
) AS d (id, name, country, lat, lng, country_iso2);

-- CIS 225 grants (catalog_owner owns the views; role booking reads them).
GRANT USAGE ON SCHEMA catalog TO booking;
GRANT SELECT ON catalog.v_tour, catalog.v_tour_day, catalog.v_tour_photo, catalog.v_destination,
    catalog.v_destination_photo, catalog.v_country TO booking;
