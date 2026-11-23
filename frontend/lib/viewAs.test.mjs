// lib/viewAs.test.mjs — AA-1157: the owner's read-only "view as a lower role" in the BFF: the proxy forwards the view
// header on reads and refuses every write itself; the start / end only go through /api/view-as (httpOnly cookie);
// the company sign-in cookies are never touched.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import {
  VIEW_AS_HEADER, cookieMaxAge, isViewEnded, minutesLeft, proxyRefusal, refusedWriteLog, viewAsHeaders, viewAsRef,
} from "./viewAs.ts";

const SID = "b1157000-0000-4000-8000-000000000001";
const read = (p) => readFileSync(new URL(`../${p}`, import.meta.url), "utf8");

test("the session id is the only thing the cookie may hold", () => {
  assert.equal(viewAsRef(SID), SID);
  assert.equal(viewAsRef("x; drop"), null);
  assert.equal(viewAsRef(undefined), null);
  assert.deepEqual(viewAsHeaders(SID), { [VIEW_AS_HEADER]: SID });
  assert.deepEqual(viewAsHeaders("junk"), {});
  assert.equal(VIEW_AS_HEADER, "x-booking-view-as");
});

test("inside a view every write is refused by the proxy; reads pass", () => {
  for (const m of ["POST", "PUT", "PATCH", "DELETE"]) assert.equal(proxyRefusal(m, "trip-cases", SID), "view_as_read_only", m);
  assert.equal(proxyRefusal("GET", "trip-cases", SID), null);
  assert.equal(proxyRefusal("POST", "trip-cases", null), null);
});

test("the view's start and end never go through the generic proxy", () => {
  assert.equal(proxyRefusal("POST", "view-as", null), "view_as_bff_only");
  assert.equal(proxyRefusal("POST", "view-as/x/end", null), "view_as_bff_only");
  assert.equal(proxyRefusal("GET", "view-as", null), null);
});

test("the end of a view and the cookie lifetime", () => {
  assert.equal(isViewEnded(409, { detail: "view_as_ended" }), true);
  assert.equal(isViewEnded(409, { detail: "conflict" }), false);
  const now = Date.parse("2026-11-23T09:00:00Z");
  assert.equal(cookieMaxAge("2026-11-23T09:15:00Z", now), 900);
  assert.equal(cookieMaxAge("2026-11-23T11:00:00Z", now), 3600);
  assert.equal(cookieMaxAge(null, now), 1);
  assert.equal(minutesLeft("2026-11-23T09:14:10Z", now), 15);
});

test("the proxy wires the refusal, the header and the cookie clear", () => {
  const proxy = read("app/api/booking/[...path]/route.ts");
  assert.match(proxy, /proxyRefusal\(req\.method, path\.join\("\/"\), viewCookie\)/);
  assert.match(proxy, /\.\.\.viewAsHeaders\(viewCookie\)/);
  assert.match(proxy, /clearViewAsCookie\(out\)/);
});

test("the view route keeps the id httpOnly and never answers it; Back acts as the owner", () => {
  const route = read("app/api/view-as/route.ts");
  assert.match(route, /const \{ session_id: sessionId, \.\.\.session \} = data;/);
  assert.match(route, /upstream\(`view-as\/\$\{ref\}\/end`, access, \{ method: "POST" \}\)/);
  const cookie = read("lib/viewAsCookie.ts");
  assert.match(cookie, /httpOnly: true, sameSite: "strict"/);
  assert.doesNotMatch(route, /setStaffSessionCookies|clearSessionCookies/);
});

test("the shell shows the banner from the profile and the layout clears an ended view", () => {
  assert.match(read("ui/local/AdminShell.tsx"), /banner=\{\(\) => <ViewAsBanner \/>\}/);
  const layout = read("app/(staff)/layout.tsx");
  assert.match(layout, /fetchStaffMe\(token, jar\.get\(VIEW_AS_COOKIE\)\?\.value\)/);
  assert.match(layout, /redirect\(VIEW_AS_CLEAR\)/);
});

test("a refused write leaves one structured log line (session, method, path; no query string)", () => {
  assert.equal(refusedWriteLog("post", "/booking/trip-cases?x=secret value", SID),
    `[view-as] booking_view_as_write_refused app=booking session=${SID} method=POST path=/booking/trip-cases`);
  assert.match(refusedWriteLog("DELETE", "/booking/a b", "junk"), /session=invalid .* path=\/booking\/a_b$/);
  const proxy = read("app/api/booking/[...path]/route.ts");
  assert.match(proxy, /if \(viewRefused === VIEW_AS_READ_ONLY\) console\.warn\(refusedWriteLog\(/);
  const route = read("app/api/view-as/route.ts");
  assert.match(route, /console\.warn\(refusedWriteLog\(req\.method, "\/booking\/view-as", current\)\)/);
});

test("the page never holds the session id; the view read always asks the API (expired views get their end row)", () => {
  assert.doesNotMatch(read("lib/api/types.ts"), /interface StaffViewAs \{[^}]*session_id/);
  const route = read("app/api/view-as/route.ts");
  assert.doesNotMatch(route, /if \(!ref\) return NextResponse\.json\(\{ active: false \}\)/);
  assert.match(route, /upstream\("view-as", access, \{ view: ref \}\)/);
  const logout = read("app/api/auth/logout/route.ts");
  assert.match(logout, /if \(cognito\) await endViews\(/);
});

test("the banner re-checks the view every 60 s and at 0 minutes, and reloads when it is over", () => {
  const banner = read("ui/local/ViewAsBanner.tsx");
  assert.match(banner, /const POLL_MS = 60_000;/);
  assert.match(banner, /setInterval\(checkView, POLL_MS\)/);
  assert.match(banner, /if \(over\) void checkView\(\)/);
  assert.match(banner, /if \(body\.active === false\) window\.location\.reload\(\)/);
});
