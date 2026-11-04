# AA-1029 R4: the Content studio Marketplace "from" price, Booking part (S268, lane portal-from)

## Decisions

- **Path**: CIS serves the Marketplace pool (`GET /v1/tours/pool`) from its own tables, so CIS attaches the price to
  each pool row by reading the Booking contract view `booking.v_from_prices` (price.from.v1). Booking grants it to the
  CIS runtime role `cis_app` (bk-040), the same consumer pattern as bk-017 / bk-023 / bk-025 / bk-035. Rejected: a new
  Booking HTTP endpoint for the studio (a second public price route, a new auth class for tenant tokens in Booking) and
  a read through AA-Partner's API (a studio-only member may have no CRM agency, and AA-Partner never owns prices).
- **`cis_app` only, not `aa_app_user`**: CIS reads the view on its main pool after the tenant block (like the job
  status read of the same route), so the tenant pool role stays without any `booking` privilege.
- **No readiness key**: no Booking code reads the grant; the CIS reader is fail soft (42P01 / 42501 = no price).

## Changed

- `test_bk017_ops_views.py` (exact `cis_app` grant set) and `test_bk039_schema.py` (asserted `cis_app` could not read
  the view) follow the new grant; bk-039's test now checks that `cis_app` reads none of the price tables.

## Tradeoffs

- One more cross-owner grant to maintain (Infra `owners.yaml` + isolation probe cell `consumer.contract_grants.bk-040`)
  against a new HTTP contract; the view is already the published contract, so the grant is the smaller change.

## Should know

- Contract file (cross-owner grant): migration-only PR; on Dev applied by Infra Migration Apply once every job table is
  idle. Merge order: this PR → Infra copy (bot) → Infra consumer-grant PR; the CIS reader can ship in any order.
