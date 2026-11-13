// e2e/visual/pages.ts
// AA-1008 (S1d/S2c, root ADR 0016 decision 9, guard G16) — the pages of the visual run. Pure data (no Playwright
// import), so ui/booking/visualPages.test.mjs can check that every app/**/page.tsx route is listed here or excluded
// with a reason, and changed-pages.mjs can map a diff to the pages it touches.
//
// auth "staff": captured by the per-PR run against `next start` with the layout-mock API (e2e/layout/fixtures.ts, the
//   unsigned session cookie the AA-910 check uses: proxy.ts only decodes it); ids are fixture ids. "public": also after
//   the production deploy (no Booking E2E sign-in from CI yet: ADR 0016 plan 4.4 hand steps H4 / H5).
// gallery: the gated kit gallery route (app/kit-gallery, KIT_GALLERY=1 servers only); selftest: its planted-overflow
//   pages, run only by the self-test step, which must FAIL.

export interface VisualPage {
  /** File-name-safe id, used in the screenshot names: `<slug>__<1440|390>__<light|dark>.png`. */
  slug: string;
  /** The app route (a page.tsx folder under app/, without route groups); dynamic segments in brackets. */
  route: string;
  /** The path to open. */
  path: string;
  auth: "public" | "staff";
  /** The page renders the kit frame (`[data-kit-frame]`, PageFrame); default true. */
  frame?: boolean;
  /** The document's expected HTTP status (default 200) on the mocked `next start` run. */
  status?: number;
  /**
   * On a deployed host (VISUAL_BASE_URL set) the page answers differently: the final status and final path after the
   * redirects Playwright follows (page.goto returns the LAST response), and why. Without it, the same as the mock.
   */
  deployed?: { status: number; finalPath: string; why: string };
  gallery?: boolean;
  /** "document" or "frame": the planted overflow the self-test expects the run to catch. */
  selftest?: "document" | "frame";
}

export const PAGES: VisualPage[] = [
  { slug: "login", route: "/login", path: "/login", auth: "public", frame: false },
  {
    slug: "not-found", route: "(not-found)", path: "/this-page-does-not-exist", auth: "public", frame: false, status: 404,
    deployed: {
      status: 200,
      finalPath: "/login",
      why: "signed out, the deployed proxy.ts sends every path but /login to /login?next=<path> (a 307), so the 404 page is only reachable with a session; the mocked run sets one and keeps the 404 check",
    },
  },
  { slug: "kit-gallery", route: "/kit-gallery", path: "/kit-gallery", auth: "public", gallery: true },
  { slug: "selftest-document", route: "/kit-gallery", path: "/kit-gallery?plant=overflow", auth: "public", gallery: true, selftest: "document" },
  { slug: "selftest-frame", route: "/kit-gallery", path: "/kit-gallery?plant=frame", auth: "public", gallery: true, selftest: "frame" },
  { slug: "dashboard", route: "/", path: "/", auth: "staff" },
  // AA-1037: Agency billing, its four tabs, the one-agency deep link CIS Tenant 360 opens, and an agency invoice.
  { slug: "agency-billing", route: "/agency-billing", path: "/agency-billing", auth: "staff" },
  {
    slug: "agency-billing-agency", route: "/agency-billing",
    path: "/agency-billing?tab=invoices&tenant=a1b2c3d4-e5f6-4a7b-8c9d-ab10cdef001a", auth: "staff",
  },
  { slug: "agency-billing-payments", route: "/agency-billing", path: "/agency-billing?tab=payments", auth: "staff" },
  { slug: "agency-billing-agencies", route: "/agency-billing", path: "/agency-billing?tab=agencies", auth: "staff" },
  { slug: "agency-billing-settings", route: "/agency-billing", path: "/agency-billing?tab=settings", auth: "staff" },
  { slug: "agency-invoice", route: "/agency-billing/invoices/[ref]", path: "/agency-billing/invoices/AAS-2026-000001", auth: "staff" },
  { slug: "agency-settlement", route: "/agency-billing/invoices/[ref]", path: "/agency-billing/invoices/AAT-2026-000001", auth: "staff" },
  { slug: "audit-log", route: "/audit-log", path: "/audit-log", auth: "staff" },
  { slug: "b2b-desk", route: "/b2b-desk", path: "/b2b-desk", auth: "staff" },
  // AA-1073 rest (bk-048): the departure board (calendar) and one departure's drawer.
  { slug: "departures", route: "/departures", path: "/departures", auth: "staff" },
  { slug: "departures-drawer", route: "/departures", path: "/departures?departure=cccc-0000-4000-8000-000000000001", auth: "staff" },
  // AA-1075: the departure manifest (counts only).
  { slug: "b2b-desk-manifest", route: "/b2b-desk/manifest/[id]", path: "/b2b-desk/manifest/00000000-0000-4000-8000-000000000b01", auth: "staff" },
  { slug: "billing", route: "/billing", path: "/billing", auth: "staff" },
  // AA-1074: Costs & profit, its three tabs, a departure's costs drawer and a purchase order drawer.
  { slug: "finance", route: "/finance", path: "/finance", auth: "staff" },
  { slug: "finance-profit-departures", route: "/finance", path: "/finance?tab=profit&view=departures", auth: "staff" },
  { slug: "finance-profit-agencies", route: "/finance", path: "/finance?tab=profit&view=agencies", auth: "staff" },
  {
    slug: "finance-departure", route: "/finance",
    path: "/finance?tab=departures&departure=d0000000-0000-4000-8000-000000000001", auth: "staff",
  },
  { slug: "finance-order", route: "/finance", path: "/finance?tab=orders&order=PO-000031", auth: "staff" },
  { slug: "cases", route: "/cases", path: "/cases", auth: "staff" },
  { slug: "case", route: "/cases/[code]", path: "/cases/TC-000001", auth: "staff" },
  { slug: "countries", route: "/catalog/countries", path: "/catalog/countries", auth: "staff" },
  { slug: "country", route: "/catalog/countries/[iso]", path: "/catalog/countries/LA", auth: "staff" },
  { slug: "destinations", route: "/catalog/destinations", path: "/catalog/destinations", auth: "staff" },
  { slug: "trip-plans", route: "/catalog/trip-plans", path: "/catalog/trip-plans", auth: "staff" },
  { slug: "trip-plan", route: "/catalog/trip-plans/[sourceId]", path: "/catalog/trip-plans/tour-00001", auth: "staff" },
  { slug: "hotels", route: "/hotels", path: "/hotels", auth: "staff" },
  { slug: "notifications", route: "/notifications", path: "/notifications", auth: "staff" },
  { slug: "pricing-rules", route: "/pricing/rules", path: "/pricing/rules", auth: "staff" },
  { slug: "agency-tiers", route: "/pricing/agency-tiers", path: "/pricing/agency-tiers", auth: "staff" },
  { slug: "agency-tariff", route: "/pricing/agency-tiers/tariff", path: "/pricing/agency-tiers/tariff?partner=pt-1", auth: "staff" },
  { slug: "price-readiness", route: "/pricing/readiness", path: "/pricing/readiness", auth: "staff" },
  { slug: "price-workbench", route: "/pricing/workbench", path: "/pricing/workbench", auth: "staff" },
  { slug: "pricing-policy", route: "/pricing/policy", path: "/pricing/policy", auth: "staff" },
  { slug: "pricing-seasons", route: "/pricing/seasons", path: "/pricing/seasons", auth: "staff" },
  { slug: "holiday-events", route: "/pricing/holiday-events", path: "/pricing/holiday-events", auth: "staff" },
  { slug: "proposals", route: "/proposals", path: "/proposals", auth: "staff" },
  { slug: "services", route: "/services", path: "/services", auth: "staff" },
  { slug: "service", route: "/services/[id]", path: "/services/SVC-000001", auth: "staff" },
  { slug: "settings", route: "/settings", path: "/settings?tab=security", auth: "staff" },
  { slug: "staff", route: "/staff", path: "/staff", auth: "staff" },
  { slug: "staff-member", route: "/staff/[cisUserId]", path: "/staff/u-001", auth: "staff" },
  { slug: "suppliers", route: "/suppliers", path: "/suppliers", auth: "staff" },
  { slug: "supplier", route: "/suppliers/[id]", path: "/suppliers/SUP-000001", auth: "staff" },
  { slug: "trip-checks", route: "/trip-checks", path: "/trip-checks", auth: "staff" },
];

/** The pages one run captures (visual.spec.ts; pure, so the unit guard tests it too). */
export function selectPages(pages: VisualPage[], opts: { auth: "public" | "staff" | "all"; slugs: string; gallery: boolean; selftest: boolean }): VisualPage[] {
  if (opts.selftest) return pages.filter((p) => p.selftest);
  const wanted = opts.slugs === "all" ? null : new Set(opts.slugs.split(",").map((s) => s.trim()).filter(Boolean));
  return pages.filter((p) => {
    if (p.selftest) return false;
    if (p.gallery) return opts.gallery; // the gallery is always captured where it exists
    if (opts.auth !== "all" && p.auth !== opts.auth) return false;
    return wanted === null || wanted.has(p.slug);
  });
}

/**
 * What one page must answer: the final document status and, when a redirect is expected, the final path. `onHost` is
 * a deployed host (VISUAL_BASE_URL set, no session); otherwise the mocked `next start` run (session cookie set).
 */
export function expectedOutcome(page: VisualPage, onHost: boolean): { status: number; finalPath: string | null } {
  if (onHost && page.deployed) return { status: page.deployed.status, finalPath: page.deployed.finalPath };
  return { status: page.status ?? 200, finalPath: null };
}

/** Routes with a page.tsx that the run does not open, each with its reason. */
export const EXCLUDED: Record<string, string> = {
  "/cases/[code]/proposals/[n]": "a proposal version: no layout-mock fixture for the version endpoints yet",
  "/cases/[code]/proposals/[n]/preview": "the customer preview of a proposal version: no layout-mock fixture for it yet",
  "/catalog/trip-plans/[sourceId]/commercial": "the commercial editor of a trip plan (writes): no layout-mock fixture for it yet",
  "/pricing/calculator": "the quote calculator posts every change to the API (writes), which the layout mock does not answer",
  "/suppliers/new": "a write form (new supplier): the layout-mock API answers reads only",
  "/suppliers/[id]/edit": "a write form (supplier edit): the layout-mock API answers reads only",
  "/suppliers/rate-sheets/[id]": "a rate sheet import: no layout-mock fixture for rate sheets yet",
};
