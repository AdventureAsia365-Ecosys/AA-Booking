// lib/paxBands.ts — AA-1175 (bk-066): the group-size bands of the computed price matrix, the pricing policy parameter
// price_pax_bands of Pricing > Policy (backend app/pricing/pax_bands.py). Stored as [[smallest, largest | null], ...];
// staff read and type them as "1, 2, 3, 4, 5-7, 8-10, 11+". No imports, so lib/paxBands.test.mjs runs it under node.
// The API checks the bands (from 1, no gap or overlap, only the last open, at most 8); this only parses the text.

/** One band: [smallest group, largest group]; null = open (the last band only). */
export type PaxBand = [number, number | null];

/** "4", "5–7" or "11+". */
export function bandLabel([low, high]: PaxBand): string {
  if (high === null) return `${low}+`;
  return high === low ? String(low) : `${low}–${high}`;
}

/** "1, 2, 3, 4, 5–7, 8–10, 11+ travellers" (the unit is the page's translated word). */
export function formatBands(bands: PaxBand[], unit: string | null): string {
  const text = bands.map(bandLabel).join(", ");
  return unit ? `${text} ${unit}` : text;
}

/** The editor text: "1, 2, 3, 4, 5-7, 8-10, 11+" (a plain hyphen, easy to type). */
export function bandsToText(bands: PaxBand[]): string {
  return bands.map(([low, high]) => (high === null ? `${low}+` : high === low ? String(low) : `${low}-${high}`)).join(", ");
}

const BAND = /^(\d+)\s*(?:([-–—])\s*(\d+)|(\+))?$/;

/** One typed band ("4", "5-7", "5–7", "11+"); null when the text is not a band. */
function parseBand(text: string): PaxBand | null {
  const m = BAND.exec(text);
  if (!m) return null;
  const low = Number(m[1]);
  if (m[4]) return [low, null];
  return [low, m[3] === undefined ? low : Number(m[3])];
}

/** The typed text as bands, or null when a part is not a band or a part other than the last is open ("5+"). */
export function textToBands(text: string): PaxBand[] | null {
  const parts = text.split(",").map((p) => p.trim()).filter(Boolean);
  if (!parts.length) return null;
  const bands = parts.map(parseBand);
  if (bands.some((b) => b === null)) return null;
  const ok = bands as PaxBand[];
  return ok.slice(0, -1).some(([, high]) => high === null) ? null : ok;
}
