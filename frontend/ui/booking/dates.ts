// ui/booking/dates.ts
// AA-996 (audit slice 1) — date and time text helpers for the Booking UI. One format everywhere (UI spec §1.2 "Dates",
// the kit formatDate / formatDateTime): "9 Oct 2026", "9 Oct 2026, 14:05", 24-hour times.
//
// Pure and import-free on purpose: the unit tests (dates.test.mjs, `npm run test:unit`) load this file with Node's
// built-in type stripping, which resolves only relative files with their extension.
//
// Date-only values ("2026-10-09": due dates, trip start, valid-from) are calendar days, not instants. Formatting them
// through `new Date("2026-10-09")` reads UTC midnight and shows the day before west of Greenwich, so they are always
// formatted in UTC here.

const ISO_DAY_RE = /^(\d{4})-(\d{2})-(\d{2})$/;

const MONTHS: Record<string, number> = {
  jan: 1, january: 1,
  feb: 2, february: 2,
  mar: 3, march: 3,
  apr: 4, april: 4,
  may: 5,
  jun: 6, june: 6,
  jul: 7, july: 7,
  aug: 8, august: 8,
  sep: 9, sept: 9, september: 9,
  oct: 10, october: 10,
  nov: 11, november: 11,
  dec: 12, december: 12,
};

function pad2(n: number): string {
  return n < 10 ? `0${n}` : String(n);
}

/** The ISO day for year / month / day, or null when that day does not exist (31 Feb). */
function toIsoDay(y: number, m: number, d: number): string | null {
  if (!Number.isInteger(y) || !Number.isInteger(m) || !Number.isInteger(d)) return null;
  if (y < 1900 || y > 2999 || m < 1 || m > 12 || d < 1) return null;
  const t = new Date(Date.UTC(y, m - 1, d));
  if (t.getUTCFullYear() !== y || t.getUTCMonth() !== m - 1 || t.getUTCDate() !== d) return null;
  return `${y}-${pad2(m)}-${pad2(d)}`;
}

/** True for a real calendar day written "YYYY-MM-DD". */
export function isIsoDay(value: string | null | undefined): value is string {
  if (!value) return false;
  const m = ISO_DAY_RE.exec(value);
  return !!m && toIsoDay(Number(m[1]), Number(m[2]), Number(m[3])) === value;
}

/** "2026-10-09" → "9 Oct 2026" (the kit formatDate output, without the time-zone shift). A full timestamp is shown
 *  as the kit formatDate shows it (the viewer's day); empty or unreadable: "—". */
export function formatDay(value: string | null | undefined): string {
  if (!value) return "—";
  if (!isIsoDay(value)) {
    if (ISO_DAY_RE.test(value)) return "—"; // shaped like a day, but not one (2026-02-30)
    const d = new Date(value);
    return Number.isNaN(d.getTime()) ? "—" : d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
  }
  const [y, m, d] = value.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });
}

/** What a person typed in a date field → "YYYY-MM-DD", "" for an empty field, null when it is not a date.
 *  Accepts day-first numbers ("28/10/2026", "28-10-2026", "28.10.26"), month names ("28 Oct 2026", "28 October
 *  2026", "Oct 28 2026") and ISO ("2026-10-28"). Never month-first numbers: "10/11/2026" is 10 November. */
export function parseDayInput(text: string): string | null {
  const t = text.trim().toLowerCase().replace(/,/g, " ").replace(/\s+/g, " ");
  if (t === "") return "";
  let m = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(t);
  if (m) return toIsoDay(Number(m[1]), Number(m[2]), Number(m[3]));
  m = /^(\d{1,2})[/.\-\s](\d{1,2})[/.\-\s](\d{2}|\d{4})$/.exec(t);
  if (m) {
    const y = m[3].length === 2 ? 2000 + Number(m[3]) : Number(m[3]);
    return toIsoDay(y, Number(m[2]), Number(m[1]));
  }
  m = /^(\d{1,2})(?:st|nd|rd|th)? ([a-z]+)\.? (\d{4})$/.exec(t);
  if (m && MONTHS[m[2]]) return toIsoDay(Number(m[3]), MONTHS[m[2]], Number(m[1]));
  m = /^([a-z]+)\.? (\d{1,2})(?:st|nd|rd|th)? (\d{4})$/.exec(t);
  if (m && MONTHS[m[1]]) return toIsoDay(Number(m[3]), MONTHS[m[1]], Number(m[2]));
  return null;
}

/** A stored time ("14:05", "14:05:00") → "14:05"; anything else → "". */
export function normalizeTime(value: string | null | undefined): string {
  if (!value) return "";
  const m = /^(\d{2}):(\d{2})(?::\d{2}(?:\.\d+)?)?$/.exec(value.trim());
  if (!m || Number(m[1]) > 23 || Number(m[2]) > 59) return "";
  return `${m[1]}:${m[2]}`;
}

/** What a person typed in a time field → "HH:MM" (24-hour), "" for an empty field, null when it is not a time.
 *  Accepts "14:05", "14.05", "14h05", "1405", "905", "9", and 12-hour input with am / pm ("2:05 pm", "2pm"). */
export function parseTimeInput(text: string): string | null {
  const t = text.trim().toLowerCase().replace(/\s+/g, "");
  if (t === "") return "";
  const m = /^(\d{1,2})(?:[:.h]?(\d{2}))?(am|pm|a\.m\.|p\.m\.)?$/.exec(t);
  if (!m) return null;
  let h = Number(m[1]);
  const min = m[2] ? Number(m[2]) : 0;
  const half = m[3] ? (m[3].startsWith("a") ? "am" : "pm") : null;
  if (min > 59) return null;
  if (half) {
    if (h < 1 || h > 12) return null;
    if (half === "am" && h === 12) h = 0;
    if (half === "pm" && h !== 12) h += 12;
  } else if (h > 23) {
    return null;
  }
  return `${pad2(h)}:${pad2(min)}`;
}

/** Instants inside free text ("…next due 2026-11-06", "…at 2026-10-27T09:30:00Z") → en-GB ("6 Nov 2026",
 *  "27 Oct 2026, 16:30" in the viewer's zone). Used for messages the API stored as plain text (notifications). */
export function localizeIsoDates(text: string): string {
  return text.replace(
    /\b(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?\b/g,
    (match, day: string, time: string | undefined, zone: string | undefined) => {
      if (!isIsoDay(day)) return match;
      if (!time) return formatDay(day);
      if (!zone) return `${formatDay(day)}, ${time}`;
      const d = new Date(match.replace(" ", "T"));
      if (Number.isNaN(d.getTime())) return match;
      return d.toLocaleString("en-GB", { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
    },
  );
}
