// ui/booking/b2cFrozen.ts — AA-1037 (root ADR 0019 d8, the AA-1034 R0 follow-up): the B2C part of Booking is
// read-only unless the build sets NEXT_PUBLIC_B2C_INTAKE=on (lib/config.ts B2C_INTAKE_OPEN). The API refuses every
// B2C write with 410 `b2b_only` while its own switch (B2C_INTAKE_ENABLED) is off; the staff admin offers none of
// those writes, by taking the matching permissions out of the staff profile the pages read with useCan:
//   - everywhere: the permissions that only write B2C records (customer invoices and payments, customers);
//   - inside a b2c trip case: also the case, version and operations writes (a B2B case keeps them).
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

/** The read-only note of a frozen B2C case and of the B2C billing pages (English UI copy, no internal ids). */
export const B2C_READ_ONLY_NOTE =
  "Read-only: Adventure Asia works with partner agencies only, so customer trips, invoices and payments can no longer be changed.";

/** A B2C billing read with no write affordance (same rule as the API's app.b2c.frozen_view): every `actions` map all
 *  false, and `invoiceable` false with the reason `b2b_only` unless a case reason is already given. The invoice and
 *  payment buttons are drawn from these flags, so the pages stay as they are and offer no refused write. */
export function frozenView<T>(value: T): T {
  if (Array.isArray(value)) return value.map((v) => frozenView(v)) as T;
  if (value === null || typeof value !== "object") return value;
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(value as Record<string, unknown>)) out[k] = frozenView(v);
  if (out.actions && typeof out.actions === "object" && !Array.isArray(out.actions)) {
    out.actions = Object.fromEntries(Object.keys(out.actions as object).map((k) => [k, false]));
  }
  if ("invoiceable" in out && (out.invoiceable || out.reason == null)) {
    out.invoiceable = false;
    out.reason = "b2b_only";
  }
  return out as T;
}
