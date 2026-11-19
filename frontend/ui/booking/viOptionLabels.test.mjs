// ui/booking/viOptionLabels.test.mjs — AA-1128 follow-up (S285): the option lists and enum labels that a live check in
// Vietnamese still showed in English (service types, rate units, hotel stars and styles, pricing channels, Booking and
// CIS roles, the trip case status and duration, "Unassigned", the open-case button name) resolve in Vietnamese through
// the phrasebook (ui/local/phrasebook.ts), and a sentence followed by an inline link keeps the space between them.
// Run: node --test ui/booking/viOptionLabels.test.mjs (`npm run test:unit` runs every ui/booking/*.test.mjs).
//
// The catalogues import "@/..." and extension-less relative paths (the Next.js resolution); a small resolve hook maps
// them to the .ts files so node's type stripping can load them. Only import-free modules and catalogues are loaded.
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { register } from "node:module";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

const FRONTEND_URL = new URL("../../", import.meta.url);
const FRONTEND = fileURLToPath(FRONTEND_URL);

const HOOKS = `
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
let base;
export function initialize(data) { base = data.base; }
export async function resolve(specifier, context, next) {
  let spec;
  if (specifier.startsWith("@/")) spec = new URL(specifier.slice(2), base).href;
  else if ((specifier.startsWith("./") || specifier.startsWith("../")) && context.parentURL && context.parentURL.endsWith(".ts"))
    spec = new URL(specifier, context.parentURL).href;
  else return next(specifier, context);
  if (!/\\.(ts|mjs|js)$/.test(spec)) {
    for (const ext of [".ts", "/index.ts"]) {
      if (existsSync(fileURLToPath(spec + ext))) { spec += ext; break; }
    }
  }
  return next(spec, context);
}
`;
register(`data:text/javascript,${encodeURIComponent(HOOKS)}`, { data: { base: FRONTEND_URL.href } });

const load = (path) => import(pathToFileURL(join(FRONTEND, path)).href);

const { phrase } = await load("ui/local/phrasebook.ts");
const { translator } = await load("ui/core/i18n.ts");
const { ROLE_LABEL, STAFF_ROLES, CIS_ROLE_LABEL, cisRoleLabel } = await load("lib/rbac.ts");
const { CHANNELS } = await load("lib/api/channels.ts");
const supplyLabels = await load("app/(staff)/_parts/supplyLabels.ts");
const { SERVICE_TYPE_LABEL, UNIT_LABEL, SERVICE_UNIT_OPTIONS, STAR_RATINGS, starsLabel, HOTEL_STYLES, hotelStyleText } = supplyLabels;
const SUPPLY = (await load("app/(staff)/_parts/supply.messages.ts")).MSG;
const ROLES = (await load("ui/booking/roles.messages.ts")).MSG;
const CHANNEL_MSG = (await load("ui/booking/channels.messages.ts")).MSG;
const CASES = (await load("app/(staff)/cases/page.messages.ts")).MSG;
await load("app/(staff)/hotels/_parts/HotelsView.messages.ts");
await load("ui/booking/StatusBadge.messages.ts");
await load("ui/booking/helperPhrases.messages.ts");
await load("ui/booking/tripPlans.messages.ts");

// Loanwords Vietnamese staff use as they are: registered, but the same text in both languages.
const SAME_IN_VI = new Set(["B2B", "Homestay", "Hostel"]);

function assertVi(label, catalogue) {
  const vi = phrase(label, "vi");
  assert.equal(typeof vi, "string", label);
  assert.notEqual(vi, "", label);
  if (SAME_IN_VI.has(label)) {
    assert.equal(typeof catalogue?.vi[label], "string", `${label} is registered`);
  } else {
    assert.notEqual(vi, label, `"${label}" has a Vietnamese text`);
  }
  assert.equal(phrase(label, "en"), label);
}

test("every Booking role and every CIS role has a Vietnamese name", () => {
  for (const r of STAFF_ROLES) assertVi(ROLE_LABEL[r], ROLES);
  for (const label of Object.values(CIS_ROLE_LABEL)) assertVi(label, ROLES);
  assert.equal(cisRoleLabel("customer_success"), "Customer Success");
  assert.equal(phrase(cisRoleLabel("customer_success"), "vi"), "Chăm sóc khách hàng");
  assert.equal(cisRoleLabel("a_future_role"), "a_future_role");
  assert.equal(cisRoleLabel("constructor"), "constructor");
});

test("every pricing channel resolves in Vietnamese, worded as on Pricing rules & FX", () => {
  const rules = readFileSync(join(FRONTEND, "app/(staff)/pricing/rules/_parts/messages.ts"), "utf8");
  for (const c of CHANNELS) {
    assertVi(c.label, CHANNEL_MSG);
    const wordings = [...rules.matchAll(new RegExp(`channel_${c.value}: "([^"]+)"`, "g"))].map((m) => m[1]);
    assert.deepEqual(wordings, [c.label, phrase(c.label, "vi")], `channel_${c.value} on the rules page`);
  }
});

test("service types, rate units and the unit options resolve in Vietnamese", () => {
  for (const label of Object.values(SERVICE_TYPE_LABEL)) assertVi(label, SUPPLY);
  for (const label of Object.values(UNIT_LABEL)) assertVi(label, SUPPLY);
  for (const o of SERVICE_UNIT_OPTIONS) assertVi(o.label, SUPPLY);
  assert.equal(phrase("per person", "vi"), "mỗi người");
  assert.equal(phrase("per group", "vi"), "mỗi nhóm");
  assert.equal(phrase("Rates in force", "vi"), "Giá đang áp dụng");
});

test("the hotel star options and the usual hotel styles resolve in Vietnamese", () => {
  assert.deepEqual(STAR_RATINGS.map(starsLabel), ["5 stars", "4 stars", "3 stars", "2 stars", "1 star"]);
  for (const n of STAR_RATINGS) assertVi(starsLabel(n), SUPPLY);
  assert.equal(phrase(starsLabel(5), "vi"), "5 sao");
  assert.equal(phrase(starsLabel(1), "vi"), "1 sao");
  for (const s of HOTEL_STYLES) assertVi(s, SUPPLY);
  assert.equal(hotelStyleText("boutique "), "Boutique");
  assert.equal(phrase(hotelStyleText("BOUTIQUE"), "vi"), "Khách sạn boutique");
  assert.equal(hotelStyleText("Riverside stilt house"), "Riverside stilt house");
  assert.notEqual(phrase("Search hotel, HTL- code, address or supplier", "vi"), "Search hotel, HTL- code, address or supplier");
});

test("the strings the live check found in English now resolve in Vietnamese", () => {
  assert.equal(phrase("Unassigned", "vi"), "Chưa phân công");
  assert.equal(phrase("Pre-departure", "vi"), "Trước khởi hành");
  assert.equal(phrase("6 days", "vi"), "6 ngày");
  assert.equal(phrase("1 day", "vi"), "1 ngày");
  assert.equal(phrase("Manual", "vi"), "Thủ công");
  assert.equal(translator(CASES, "vi")("Open {0}", "TC-000028"), "Mở TC-000028");
  assert.equal(translator(CASES, "en")("Open {0}", "TC-000028"), "Open TC-000028");
});

function tsxFiles(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...tsxFiles(p));
    else if (name.endsWith(".tsx")) out.push(p);
  }
  return out;
}

test("a sentence followed by an inline link on the next line keeps a space ({\" \"}) in both languages", () => {
  // JSX drops the line break between `{t("… approved.")}` and `<Link>`: "approved.Add or approve rates inPricing rules".
  const glued = /\{t[rx]?\("[^"\n]*[A-Za-z.:]"[^)\n]*\)\}\n[ \t]*<(?:Link|a)\b/g;
  const found = [];
  for (const root of ["app", "ui/booking", "ui/local"]) {
    for (const file of tsxFiles(join(FRONTEND, root))) {
      for (const m of readFileSync(file, "utf8").matchAll(glued)) found.push(`${file.slice(FRONTEND.length)}: ${m[0].split("\n")[0].trim()}`);
    }
  }
  assert.deepEqual(found, []);
});
