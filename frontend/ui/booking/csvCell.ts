// ui/booking/csvCell.ts
// AA-996 (review fix) — spreadsheet formula guard for the Booking CSV exports. Free text in a list (customer names
// from public TripPlanner inquiries, proposal titles, references) can start with "=", "+", "-" or "@"; Excel and
// Google Sheets run such a cell as a formula when staff open the file (CSV / formula injection, OWASP). The kit
// writer (ui/csv.ts, a CIS copy) quotes cells but does not neutralise formulas, so every Booking export passes its
// values through `csvCell` first: a risky string gets a leading single quote, which the spreadsheet shows as text.
// Numbers and plain amounts ("-50.00", "-50.00 USD") stay as they are, so refunds keep reading as numbers.
//
// Pure and import-free on purpose (unit-tested with Node's type stripping, like dates.ts).

const RISKY_START = /^[=+\-@\t\r]/;
const AMOUNT = /^[-+]?\d+(?:\.\d+)?(?: [A-Z]{3})?$/;

/** A CSV cell value that a spreadsheet will not run as a formula. Non-strings are returned unchanged. */
export function csvCell(value: unknown): unknown {
  if (typeof value !== "string") return value;
  if (!RISKY_START.test(value) || AMOUNT.test(value)) return value;
  return `'${value}`;
}

/** The same columns with every value passed through `csvCell`. */
export function safeCsvColumns<T>(columns: { header: string; value: (row: T) => unknown }[]): { header: string; value: (row: T) => unknown }[] {
  return columns.map((c) => ({ header: String(csvCell(c.header)), value: (row: T) => csvCell(c.value(row)) }));
}
