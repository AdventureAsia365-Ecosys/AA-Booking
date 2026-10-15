-- CI-only stub of the CIS catalog read model Booking reads (shape: AA-CIS-App api/migrations/225_catalog_views.sql,
-- catalog.v_destination over shared.destinations) plus the grant CIS 225 gives role booking.
-- Applied after the bk-NNN migrations, since the grant needs role booking.
CREATE SCHEMA IF NOT EXISTS catalog;

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

-- CIS 225
GRANT USAGE ON SCHEMA catalog TO booking;
GRANT SELECT ON catalog.v_destination TO booking;
