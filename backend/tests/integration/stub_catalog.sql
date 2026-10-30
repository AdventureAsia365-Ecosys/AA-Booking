-- CI-only stub of the CIS catalog read model Booking reads (shape: AA-Ecosys docs/architecture/db-schema-reference.md
-- and AA-CIS-App api/migrations/225_catalog_views.sql, only the columns Booking selects) plus the grant CIS 225 gives
-- role booking. catalog.v_tour / v_tour_photo (AA-679 trip plans) are plain tables standing in for the views;
-- catalog.v_destination (AA-822 supplier place picker) is a view over fixed rows; v_destination_photo and
-- v_country (AA-996 countries) are plain tables. Applied after the bk-NNN migrations (the grant needs role booking).
-- Live invariants the stub keeps (AA-996 list hotfix): every v_tour_day / v_tour_photo row carries the tour_id of its
-- live v_tour row (a trigger fills it when a test leaves it out), and catalog.v_tour_place is the per-place source of
-- v_tour_day.places (CIS 225: places = array_agg(place_name ORDER BY position) per tour and day), here derived back
-- from the stub's day rows.
-- AA-996 perf guard: the parse-backed views have the live PLAN shape. On Dev catalog._tour_day_text (CIS 289) parses
-- every live itinerary and is DISTINCT ON (tour_id, day_number) behind a security barrier, so only a tour_id filter
-- reaches the parse; a source_id filter is applied after every itinerary was parsed (~1.5 s a read, #130 -> 503).
-- Here catalog._tour_day_text is the same DISTINCT ON over catalog.stub_parsed_day (the rows the tests write through
-- v_tour_day) plus catalog.stub_parsed_day_noise (fixed itineraries of tours not in v_tour, never read by a correct
-- query). A scan of either base table without a tour_id condition is the #130 class: tests/catalog_perf.py finds it in
-- EXPLAIN. Tests write days through catalog.v_tour_day (INSTEAD OF triggers) and TRUNCATE catalog.stub_parsed_day.
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

-- The parsed itinerary days. ord keeps insertion order: a repeated (tour, day) keeps the LAST block, like CIS 289.
CREATE TABLE IF NOT EXISTS catalog.stub_parsed_day (
    ord            BIGSERIAL PRIMARY KEY,
    source_system  TEXT NOT NULL DEFAULT 'cis',
    source_id      TEXT NOT NULL,
    tour_id        UUID,
    day_number     INTEGER NOT NULL,
    title          TEXT,
    body           TEXT,
    places         TEXT[]
);
CREATE TABLE IF NOT EXISTS catalog.stub_parsed_day_noise (LIKE catalog.stub_parsed_day INCLUDING DEFAULTS);
-- 200 tours x 4 days that are not in catalog.v_tour: a read that is not keyed by tour_id parses them all.
INSERT INTO catalog.stub_parsed_day_noise (source_id, tour_id, day_number, title, body, places)
SELECT 'stub-noise-' || t, ('00000000-0000-4000-9000-' || lpad(t::text, 12, '0'))::uuid, d, 'Noise day ' || d, '',
       ARRAY['Noise ' || t]
FROM generate_series(1, 200) AS t CROSS JOIN generate_series(1, 4) AS d
WHERE NOT EXISTS (SELECT 1 FROM catalog.stub_parsed_day_noise);

CREATE OR REPLACE VIEW catalog._tour_day_text WITH (security_barrier = true) AS
SELECT DISTINCT ON (b.tour_id, b.day_number)
       b.source_system, b.source_id, b.tour_id, b.day_number, b.title, b.body, b.places
FROM (
    SELECT ord, source_system, source_id, tour_id, day_number, title, body, places FROM catalog.stub_parsed_day
    UNION ALL
    SELECT ord, source_system, source_id, tour_id, day_number, title, body, places FROM catalog.stub_parsed_day_noise
) b
ORDER BY b.tour_id, b.day_number, b.ord DESC;

CREATE OR REPLACE VIEW catalog.v_tour_day WITH (security_barrier = true) AS
SELECT d.source_system, d.source_id, d.tour_id, d.day_number, d.title, d.body, d.places
FROM catalog._tour_day_text d;

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
-- place's country text; NULL when it cannot). Kazakhstan is a CIS country outside booking.countries (bk-005 / bk-008 /
-- bk-031: Pakistan, Taiwan and Kyrgyzstan joined it in bk-031).
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
    ('KG', 'KGZ', '417', 'Kyrgyzstan', 'Kyrgyzstan'),
    ('UZ', 'UZB', '860', 'Uzbekistan', 'Uzbekistan'),
    ('KZ', 'KAZ', '398', 'Kazakhstan', 'Kazakhstan')
ON CONFLICT (iso2) DO NOTHING;

-- AA-997: CIS geo_status (228) and duplicate_of (257) of the fixed places; no row = 'ok' and not merged. Tests write
-- here to hide (suspect) or merge a place and TRUNCATE it after.
CREATE TABLE IF NOT EXISTS catalog.stub_destination_state (
    destination_id  UUID PRIMARY KEY,
    geo_status      TEXT NOT NULL DEFAULT 'ok',
    duplicate_of    UUID
);

CREATE OR REPLACE VIEW catalog.v_destination AS
SELECT 'cis'::text AS source_system, d.id::text AS source_id, d.id AS destination_id, d.name, d.country,
       d.lat, d.lng, NULL::text AS cover_photo_path, NULL::text AS cover_image_url, 0::bigint AS photo_count,
       now() AS created_at, now() AS updated_at, d.country_iso2,
       coalesce((SELECT s.geo_status FROM catalog.stub_destination_state s WHERE s.destination_id = d.id), 'ok')
           AS geo_status
FROM (VALUES
    ('d0000000-0000-4000-8000-000000000001'::uuid, 'Hanoi', 'Vietnam', 21.03::float8, 105.85::float8, 'VN'::char(2)),
    ('d0000000-0000-4000-8000-000000000002'::uuid, 'Luang Prabang', 'Laos', 19.89::float8, 102.13::float8, 'LA'),
    ('d0000000-0000-4000-8000-000000000003'::uuid, 'Hoi An', 'Vietnam', 15.88::float8, 108.33::float8, 'VN'),
    ('d0000000-0000-4000-8000-000000000004'::uuid, 'Atlantis', 'Nowhere', 0::float8, 0::float8, NULL),
    ('d0000000-0000-4000-8000-000000000005'::uuid, 'Lahore', 'Pakistan', 31.55::float8, 74.34::float8, 'PK')
) AS d (id, name, country, lat, lng, country_iso2);

-- CIS 257: a merged destination id -> its keeper (the merged row stays in v_destination).
CREATE OR REPLACE VIEW catalog.v_destination_redirect AS
SELECT s.destination_id, s.duplicate_of AS keeper_id
FROM catalog.stub_destination_state s
WHERE s.duplicate_of IS NOT NULL;

-- The live views carry the live tour's tour_id on every day and photo row.
CREATE OR REPLACE FUNCTION catalog.stub_fill_tour_id() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.tour_id IS NULL THEN
        SELECT t.tour_id INTO NEW.tour_id FROM catalog.v_tour t
        WHERE t.source_system = NEW.source_system AND t.source_id = NEW.source_id;
    END IF;
    RETURN NEW;
END $$;
-- Tests write day rows through catalog.v_tour_day as they did when it was a table.
CREATE OR REPLACE FUNCTION catalog.stub_write_day() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        DELETE FROM catalog.stub_parsed_day s
        WHERE s.source_system = OLD.source_system AND s.source_id = OLD.source_id AND s.day_number = OLD.day_number;
        RETURN OLD;
    END IF;
    IF NEW.tour_id IS NULL THEN
        SELECT t.tour_id INTO NEW.tour_id FROM catalog.v_tour t
        WHERE t.source_system = coalesce(NEW.source_system, 'cis') AND t.source_id = NEW.source_id;
    END IF;
    INSERT INTO catalog.stub_parsed_day (source_system, source_id, tour_id, day_number, title, body, places)
    VALUES (coalesce(NEW.source_system, 'cis'), NEW.source_id, NEW.tour_id, NEW.day_number, NEW.title, NEW.body,
            NEW.places);
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS stub_write_day ON catalog.v_tour_day;
CREATE TRIGGER stub_write_day INSTEAD OF INSERT OR DELETE ON catalog.v_tour_day
    FOR EACH ROW EXECUTE FUNCTION catalog.stub_write_day();
DROP TRIGGER IF EXISTS stub_fill_tour_id ON catalog.v_tour_photo;
CREATE TRIGGER stub_fill_tour_id BEFORE INSERT ON catalog.v_tour_photo
    FOR EACH ROW EXECUTE FUNCTION catalog.stub_fill_tour_id();

-- catalog.v_tour_place (CIS 225/256): one row per tour, day and place; only the columns Booking reads. Over the
-- parsed days like v_tour_day.places (security barrier: only a tour_id filter reaches the parse).
CREATE OR REPLACE VIEW catalog.v_tour_place WITH (security_barrier = true) AS
SELECT d.source_system, d.source_id, d.tour_id, d.day_number::smallint AS day_number, u.ord AS position,
       u.place AS place_name
FROM catalog._tour_day_text d CROSS JOIN LATERAL unnest(d.places) WITH ORDINALITY AS u(place, ord);

-- catalog.v_tour_place_name (CIS 298, AA-996 S256): row for row the names of v_tour_place (source_system, source_id,
-- tour_id, day_number, position, place_name) without its place card / destination / segment lookups; the trip-plans
-- list reads its destinations here. Same parse-backed shape (only a tour_id filter reaches the parse).
CREATE OR REPLACE VIEW catalog.v_tour_place_name WITH (security_barrier = true) AS
SELECT p.source_system, p.source_id, p.tour_id, p.day_number, p.position, p.place_name
FROM catalog.v_tour_place p;

-- CIS 225 grants (catalog_owner owns the views; role booking reads them).
GRANT USAGE ON SCHEMA catalog TO booking;
GRANT SELECT ON catalog.v_tour, catalog.v_tour_day, catalog.v_tour_photo, catalog.v_tour_place,
    catalog.v_tour_place_name, catalog.v_destination, catalog.v_destination_photo, catalog.v_country,
    catalog.v_destination_redirect TO booking;
