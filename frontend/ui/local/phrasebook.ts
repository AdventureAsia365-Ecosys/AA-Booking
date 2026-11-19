// ui/local/phrasebook.ts — AA-1128 (B5, S283 UI audit): the Booking staff app's phrasebook, the Vietnamese side of every
// message catalogue of the app, keyed by its English text.
//
// The pages show text through the kit catalogues (core/i18n.ts `defineMessages`, one `<File>.messages.ts` next to each
// file, the keys being the English text) and `useT`. Some English text lives in module-level tables (status labels,
// nav entries, column headers, option lists) or is built by import-free helpers ("7 days / 6 nights"), and a component
// reads it at render time, often in another file. Each catalogue registers itself here
// (`phrasebook(defineMessages(...))`), so such a value can be shown in the UI language with `useTx()`
// (ui/local/useTx.ts) where it is rendered, without a hook at module level:
//   - a string message is matched exactly ("Pending" → "Chờ duyệt");
//   - a function message ("{0} days / {1} nights") is matched as a pattern, but only when every part it fills in is a
//     number, an amount, an ISO day or currency codes ("7", "1,250.00", "$1,250", "VND 1.250.000", "2026-12-01", "EUR, GBP"), so a tour title or
//     a name that happens to contain the same words is never rewritten.
// Text that matches nothing (data: names, titles, codes) comes back unchanged.
//
// No React and no "use client": a server component may import a catalogue (the staff layout's notices).

import type { Catalog, Lang, Messages } from "../core/i18n";

const VI = new Map<string, string>();

interface Pattern {
  /** The longest literal piece of the English template: a cheap first check before the regex. */
  anchor: string;
  re: RegExp;
  /** The Vietnamese template: literal pieces and argument numbers. */
  vi: (string | number)[];
}
const PATTERNS: Pattern[] = [];
const PATTERN_KEYS = new Map<string, { viT: string; pattern: Pattern }>();

const MARK = (i: number) => `\u0001${i}\u0002`;
const MARK_RE = /\u0001(\d)\u0002/g;
const MAX_ARGS = 6;
/** What a pattern may fill in: digits with separators, a sign, %, a currency symbol or a 3-letter code. */
const FILLER = /^(?:[-+−]?(?:[A-Z]{3}\s?)?[$€£¥₫]?\s?\d[\d\s.,:/'’-]*(?:\s?(?:%|[$€£¥₫]|[A-Z]{3}))?|[A-Z]{3}(?:, [A-Z]{3})*)$/;

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function template(fn: (...a: unknown[]) => string): string | null {
  try {
    return fn(...Array.from({ length: MAX_ARGS }, (_, i) => MARK(i)));
  } catch {
    return null;
  }
}

function addPattern(key: string, en: (...a: unknown[]) => string, vi: (...a: unknown[]) => string): void {
  const enT = template(en);
  const viT = template(vi);
  if (!enT || !viT) return;
  // Same rule as the plain strings: on a second wording of the same key, the Vietnamese template that sorts first wins.
  const had = PATTERN_KEYS.get(key);
  if (had !== undefined) {
    if (viT >= had.viT) return;
    PATTERNS.splice(PATTERNS.indexOf(had.pattern), 1);
  }
  const pieces = enT.split(MARK_RE); // literal, index, literal, index, ...
  const literals = pieces.filter((_, i) => i % 2 === 0);
  const indexes = pieces.filter((_, i) => i % 2 === 1).map(Number);
  if (!indexes.length || new Set(indexes).size !== indexes.length) return;
  // At least three letters of literal text, so "{0} {1}" or "{0}: {1}" never becomes a catch-all.
  if ((literals.join("").match(/[A-Za-z]/g) ?? []).length < 3) return;
  // Two fillers side by side cannot be told apart.
  if (literals.slice(1, -1).some((l) => l === "")) return;
  const re = new RegExp(`^${literals.map(escapeRe).join("(.+?)")}$`);
  const viPieces = viT.split(MARK_RE).map((p, i) => (i % 2 === 1 ? Number(p) : p));
  const anchor = literals.reduce((a, b) => (b.length > a.length ? b : a), "");
  const pattern: Pattern = { anchor, re, vi: viPieces.map((p, i) => (i % 2 === 1 ? indexes.indexOf(p as number) : p)) };
  PATTERN_KEYS.set(key, { viT, pattern });
  PATTERNS.push(pattern);
  // Longer literal text first: "{0} adults, {1} children" before "{0} adults".
  PATTERNS.sort((a, b) => b.re.source.length - a.re.source.length || (a.re.source < b.re.source ? -1 : a.re.source > b.re.source ? 1 : 0));
}

/** Register a catalogue's Vietnamese text by its English key; returns the catalogue unchanged. */
export function phrasebook<E extends Catalog>(messages: Messages<E>): Messages<E> {
  const en = messages.en as Record<string, unknown>;
  for (const [key, value] of Object.entries(messages.vi as Record<string, unknown>)) {
    if (typeof value === "string") {
      // Two catalogues may word one English text differently: keep the one that sorts first, so the pick never
      // depends on the order the modules were loaded in (the server keeps every page's modules, a browser only the
      // pages it opened).
      const had = VI.get(key);
      if (value !== "" && (had === undefined || value < had)) VI.set(key, value);
    } else if (typeof value === "function" && typeof en[key] === "function") {
      addPattern(key, en[key] as (...a: unknown[]) => string, value as (...a: unknown[]) => string);
    }
  }
  return messages;
}

function matchPattern(text: string): string | null {
  for (const p of PATTERNS) {
    if (!text.includes(p.anchor)) continue;
    const m = p.re.exec(text);
    if (!m) continue;
    const args = m.slice(1);
    if (!args.every((a) => FILLER.test(a.trim()))) continue;
    return p.vi.map((piece, i) => (i % 2 === 1 ? (piece === -1 ? "" : args[piece as number]) : piece)).join("");
  }
  return null;
}

/** An English UI string in a language: its registered Vietnamese text, or the string itself (data, unknown text). */
export function phrase<S extends string | null | undefined>(text: S, lang: Lang): S {
  if (lang !== "vi" || typeof text !== "string" || text === "") return text;
  const exact = VI.get(text);
  if (exact !== undefined) return exact as S;
  return (matchPattern(text) ?? text) as S;
}

/** Test hook: how many phrases and patterns are registered. */
export function phrasebookSize(): { phrases: number; patterns: number } {
  return { phrases: VI.size, patterns: PATTERNS.length };
}
