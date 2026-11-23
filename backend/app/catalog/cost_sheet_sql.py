"""The SQL of the cost sheet's job inputs (split out of :mod:`app.catalog.cost_sheet`, file size limit; AA-1175): a
plan's components, the plans a run recomputes and the watermark that queues a run. ``cost_sheet`` re-exports them."""

COMPONENTS_SQL = """
SELECT c.day_no, c.hotel_room_type_id::text AS room_type_id, c.service_id::text AS service_id, rt.max_occupancy,
       c.meal_plan,
       coalesce(rt.status, s.status) AS status, coalesce(h.status, 'active') AS hotel_status
FROM booking.trip_plan_components c
LEFT JOIN booking.hotel_room_types rt ON rt.id = c.hotel_room_type_id
LEFT JOIN booking.hotels h ON h.id = rt.hotel_id
LEFT JOIN booking.services s ON s.id = c.service_id
WHERE c.trip_plan_id = $1::uuid
ORDER BY c.day_no, coalesce(c.hotel_room_type_id, c.service_id)
"""
PLANS_SQL = """
SELECT p.id::text AS id, p.source_id FROM booking.trip_plans p
WHERE ($1::uuid IS NULL OR p.id = $1::uuid)
  AND (EXISTS (SELECT 1 FROM booking.trip_plan_components c WHERE c.trip_plan_id = p.id)
       OR EXISTS (SELECT 1 FROM booking.trip_plan_prices r
                  WHERE r.trip_plan_id = p.id AND (r.computed OR r.channel = 'b2b'))
       OR EXISTS (SELECT 1 FROM booking.price_list_versions v
                  WHERE v.trip_plan_id = p.id AND v.status IN ('published', 'superseded')))
ORDER BY p.source_id
"""
# What a recompute depends on; a change of any part queues one run (the idempotency key is the hash).
WATERMARK_SQL = """
SELECT concat_ws('|',
    (SELECT count(*) || ':' || coalesce(max(approved_at)::text, '') FROM booking.policy_approvals),
    (SELECT count(*) || ':' || coalesce(max(created_at)::text, '') FROM booking.pricing_policy_events
     WHERE event = 'approved'),
    (SELECT count(*) || ':' || coalesce(max(updated_at)::text, '') FROM booking.supplier_contracts),
    (SELECT count(*) || ':' || coalesce(max(updated_at)::text, '') FROM booking.supplier_rates),
    (SELECT count(*) || ':' || coalesce(max(updated_at)::text, '') FROM booking.trip_plans),
    (SELECT count(*) FROM booking.trip_plan_components),
    (SELECT count(*) || ':' || coalesce(max(updated_at)::text, '') FROM booking.price_list_versions),
    (SELECT count(*) || ':' || coalesce(max(greatest(decided_at, ended_at))::text, '') FROM booking.season_ranges),
    (SELECT count(*) || ':' || coalesce(max(greatest(decided_at, ended_at))::text, '') FROM booking.holiday_events),
    (SELECT count(*) || ':' || coalesce(max(updated_at)::text, '') FROM booking.season_calendars),
    (now() AT TIME ZONE 'UTC')::date::text) AS mark
"""
