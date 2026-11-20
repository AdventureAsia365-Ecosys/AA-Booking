# AA-1155 T4.1 — Booking approve split (lane l6, S286)

Root spec `.kiro/specs/AA-1155-custom-staff-roles` task T4.1, requirement R4.3, design D4. No migration.

## Decisions

- **New capability `pricing.approve`** = admin, contracting (exactly the holders of `pricing.write` today, so no
  built-in role loses a pricing right). It gates: pricing rule, FX rate and cancellation policy approve and reject;
  agency tier discount and rate card approve; departure price adjustment approve, and ending another person's
  pending adjustment or one in force.
- **Ending a price adjustment or an allotment (reviewer fix).** `end` rejects a pending row (a checker step) or
  withdraws one in force (a decision), and both modules use it for the maker's own pending row too. The first cut
  gated the whole route with the approval, which took the maker's withdrawal away (a regression). Now the end routes
  keep the write right (`pricing.write`, `b2b_desk.write`) and the service reads the locked row and decides
  (`may_end`): an approval holder ends any row; anyone else only its own PENDING row, with the write right (same
  pattern as `can_withdraw` on pricing rows). So this check runs after the row read, not before it; approve stays
  "before any read". The DB end reason of a pending row stays `rejected` (the bk-048 / bk-046 CHECK allows
  `withdrawn` only for an approved row); the UI labels the maker's button "Withdraw".
- **Allotment approve moves to the existing `b2b_desk.approve`** (admin, contracting; it already gated desk
  bookings below the floor and departure fees), and so does ending another person's allotment or one in force.
  Proposing and withdrawing one's own pending allotment stay `b2b_desk.write`.
- **Hotel and service activate need `supplier.activate`**: AA-1150 had not re-gated them. The route keeps
  `supply.write` (like `POST /suppliers/{ref}/activate`) and `app.supply.records.set_status` refuses a non-holder
  before it reads the record. Deactivate stays `supply.write`.
- **Two layers, like AA-1150:** the route dependency (`require(...)`) and a check at the top of the service function
  (`require_approve`), both 403 `forbidden`, before any SQL (except ending an adjustment / allotment, see above). Reject and withdraw share `close_row`: only
  `event="rejected"` is gated, so the maker keeps withdrawing its own row with `pricing.write`.
- **Row flags follow the capability:** `can_approve` (pricing rows, tier discounts, rate cards, cancellation
  policies, price adjustments) and `can_reject` now test `pricing.approve`; `can_withdraw` keeps `pricing.write`.
- **Frontend:** the pricing pages already follow the per-row flags. The pages that test a capability themselves now
  test the approval one: departure board price adjustment buttons (`pricing.approve`; the maker's own pending row
  keeps a Withdraw button with `pricing.write`), trip plan allotment buttons (`b2b_desk.approve`; own pending row:
  Withdraw with `b2b_desk.write`; both pages ask one pure helper, `lib/approveSplit.ts` `endAction`, which mirrors
  the API's `may_end` and keeps the row components under their code-health complexity baseline), hotel / service Activate (`supplier.activate`, via `useRecordStatus().canToggle`). The
  hotel drawer and the service page share a new `RecordStatusButton` (the code-health baseline may not grow, and
  the inline button plus the new condition pushed both over it; HotelDrawer is now under the limit).
  `lib/rbac.ts` holds page rules only (the pages stay open to the same roles), so it is not changed; buttons use
  `useCan()` (me.permissions), as AA-1150 did.

## Changed

- vs the task text: "Frontend rbac.ts" — no edit was needed in `rbac.ts` (see above), which also keeps clear of
  lane l7's edits there.
- Built-in `sales` no longer approves an allotment or ends another person's or one in force (it held
  `b2b_desk.write`). It still proposes one and withdraws its own pending one. Admin and contracting keep every right.
- **Hotel / service create (reviewer fix).** `app.supply.records.create_record` now writes `status` explicitly:
  `active` for a holder of `supplier.activate`, `inactive` otherwise (the column default is `active`, bk-010), as
  AA-1150 did for suppliers. Admin and contracting (incl. the A10e-3 E2E pricing maker, a `booking-contracting`
  member) still create active records; the audit summary says "(inactive until approved)" otherwise.
- Existing tests updated where sales or a generic writer ended an allotment
  (`tests/unit/test_aa1073_departures.py`, `tests/integration/test_aa1073_departures.py`).
- `e2e/layout/staff-me.json` (the layout fixture admin) gains `pricing.approve` and `supplier.activate`, as the real
  admin has them.

## Tradeoffs

- Route dependency changed to the approval capability (instead of keeping the write capability and checking only in
  the service) for the pricing and allotment decisions: a holder of the approval is always a holder of the write
  today (unit test pins `approve <= write`), and the OpenAPI shape does not change. The two end routes are the
  exception: they keep the write right because the maker's own pending row is a withdrawal, not a decision, and
  only the row says which (no approve-only role exists: custom roles never hold an approval, root spec D4).

## Should know

- Unit: `tests/unit/test_aa1155_approve_split.py` (matrix, every gated route 403 for a maker role with every
  contracting right but the approval class, 404 = past the gate for admin / contracting, sales / finance / content
  403, service-level refusals before any SQL, row flags). Integration on PostgreSQL:
  `tests/integration/test_aa1155_approve_split.py` (contracting stripped of the three approvals makes every row and is
  refused every decision; it withdraws its own pending allotment and price adjustment but not another person's nor
  one in force; its new hotel and service start inactive, an admin's active; an admin decides; built-in contracting
  approves an admin's rows and creates active records; sales 403 on allotment approve and on another person's
  allotment, 200 withdrawing its own pending one). Frontend: `lib/approveSplit.test.mjs` (the maker's own-pending
  Withdraw button on both pages, `endAction` truth table). `docs/api/openapi.json` regenerated (the two end routes
  gained a description).
- The A10e-2 pricing checker is `booking-contracting`, so it keeps every approval of this split; its own routes
  (price versions) are untouched.
