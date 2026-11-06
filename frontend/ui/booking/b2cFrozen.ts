// ui/booking/b2cFrozen.ts — AA-1037 / AA-1046 (root ADR 0019 d8): the B2C half of Booking is retired, so a b2c trip
// case is read-only history (lib/config.ts B2C_INTAKE_OPEN is false for good; the API refuses every write on one with
// 410 `b2b_only`). The staff admin offers none of those writes, by taking the matching permissions out of the staff
// profile the pages read with useCan:
//   - everywhere: the permissions that only wrote B2C records (customer invoices and payments, customers: their pages
//     are gone, the permissions stay in the API's role table);
//   - inside a b2c trip case: also the case, version and operations writes (a B2B case keeps them).
// The `open` parameters stay so the rules read the same as before; every caller passes B2C_INTAKE_OPEN (false).
// Pure (no React), so ui/booking/b2cFrozen.test.mjs tests it with node --test.

/** Permissions that only ever write B2C records: `booking.invoices` / `payments` are B2C-only, and so are customers. */
export const B2C_ONLY_WRITES: readonly string[] = ["billing.write", "payment.verify", "customer.write", "customer.admin"];

/** Writes that are B2C on a b2c trip case: its status, notes, assignment, proposals, supplier bookings and checks. */
export const B2C_CASE_WRITES: readonly string[] = ["trip_case.write", "trip_case.assign", "version.write", "ops.write"];

/** The staff permissions without the B2C writes while the B2C part is closed (`open` false); unchanged when open. */
export function withoutB2cWrites(permissions: string[], open: boolean): string[] {
  return open ? permissions : permissions.filter((p) => !B2C_ONLY_WRITES.includes(p));
}

/** True when a trip case is a frozen B2C case (channel b2c while the B2C part is closed). */
export function isFrozenCase(channel: string | null | undefined, open: boolean): boolean {
  return !open && channel !== "b2b";
}

/** The permissions inside one trip case: a frozen B2C case also loses the case, version and operations writes. */
export function casePermissions(permissions: string[], channel: string | null | undefined, open: boolean): string[] {
  const base = withoutB2cWrites(permissions, open);
  return isFrozenCase(channel, open) ? base.filter((p) => !B2C_CASE_WRITES.includes(p)) : base;
}

/** The read-only note of a b2c case (English UI copy, no internal ids). */
export const B2C_READ_ONLY_NOTE =
  "Read-only: Adventure Asia works with partner agencies only, so this traveller trip is kept as history and can no longer be changed.";
