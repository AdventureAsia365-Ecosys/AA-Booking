# AA-1155 T4.1 — Booking approve split (lane l6, S286)

Root spec `.kiro/specs/AA-1155-custom-staff-roles` task T4.1, requirement R4.3, design D4. No migration.

## Decisions

- **New capability `pricing.approve`** = admin, contracting (exactly the holders of `pricing.write` today, so no
  built-in role loses a pricing right). It gates: pricing rule, FX rate and cancellation policy approve and reject;
  agency tier discount and rate card approve; departure price adjustment approve **and end**.
- **Price adjustment end needs `pricing.approve`.** `end` rejects a pending adjustment (a checker step) or withdraws
  one in force (a price decision). The service cannot tell which before it reads the row, and the spec says "before
  any read", so the whole route is an approval. The maker of a pending adjustment can no longer take it back itself
  (no maker-only withdraw exists for adjustments, unlike pricing rules); a holder ends it.
- **Allotment approve and end move to the existing `b2b_desk.approve`** (admin, contracting; it already gated desk
  bookings below the floor and departure fees). Proposing stays `b2b_desk.write`.
- **Hotel and service activate need `supplier.activate`**: AA-1150 had not re-gated them. The route keeps
  `supply.write` (like `POST /suppliers/{ref}/activate`) and `app.supply.records.set_status` refuses a non-holder
  before it reads the record. Deactivate stays `supply.write`.
- **Two layers, like AA-1150:** the route dependency (`require(...)`) and a check at the top of the service function
  (`require_approve`), both 403 `forbidden`, before any SQL. Reject and withdraw share `close_row`: only
  `event="rejected"` is gated, so the maker keeps withdrawing its own row with `pricing.write`.
- **Row flags follow the capability:** `can_approve` (pricing rows, tier discounts, rate cards, cancellation
  policies, price adjustments) and `can_reject` now test `pricing.approve`; `can_withdraw` keeps `pricing.write`.
- **Frontend:** the pricing pages already follow the per-row flags. The pages that test a capability themselves now
  test the approval one: departure board price adjustment buttons (`pricing.approve`), trip plan allotment buttons
  (`b2b_desk.approve`), hotel / service Activate (`supplier.activate`, via `useRecordStatus().canToggle`). The
  hotel drawer and the service page share a new `RecordStatusButton` (the code-health baseline may not grow, and
  the inline button plus the new condition pushed both over it; HotelDrawer is now under the limit).
  `lib/rbac.ts` holds page rules only (the pages stay open to the same roles), so it is not changed; buttons use
  `useCan()` (me.permissions), as AA-1150 did.

## Changed

- vs the task text: "Frontend rbac.ts" — no edit was needed in `rbac.ts` (see above), which also keeps clear of
  lane l7's edits there.
- Built-in `sales` no longer approves or ends an allotment (it held `b2b_desk.write`). It still proposes one. This is
  the spec's "allotment approve and end move to `b2b_desk.approve`"; admin and contracting keep every right.
- Existing tests updated where sales or a generic writer ended an allotment
  (`tests/unit/test_aa1073_departures.py`, `tests/integration/test_aa1073_departures.py`).
- `e2e/layout/staff-me.json` (the layout fixture admin) gains `pricing.approve` and `supplier.activate`, as the real
  admin has them.

## Tradeoffs

- Route dependency changed to the approval capability (instead of keeping the write capability and checking only in
  the service) for the pricing and allotment decisions: a holder of the approval is always a holder of the write
  today (unit test pins `approve <= write`), and the OpenAPI shape does not change.
- Hotel / service create still writes the column default `active`. A custom maker could create an active hotel under
  an already-approved supplier. AA-1150 closed that gap for suppliers; for hotels and services it is outside T4.1 and is
  raised as a spec follow-up rather than changed silently.

## Should know

- Unit: `tests/unit/test_aa1155_approve_split.py` (matrix, every gated route 403 for a maker role with every
  contracting right but the approval class, 404 = past the gate for admin / contracting, sales / finance / content
  403, service-level refusals before any SQL, row flags). Integration on PostgreSQL:
  `tests/integration/test_aa1155_approve_split.py` (contracting stripped of the three approvals makes every row and is
  refused every decision; an admin decides; built-in contracting approves an admin's rows; sales 403 on allotment
  decisions). Frontend: `lib/approveSplit.test.mjs`.
- The A10e-2 pricing checker is `booking-contracting`, so it keeps every approval of this split; its own routes
  (price versions) are untouched.
