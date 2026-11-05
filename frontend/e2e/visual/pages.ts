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
  /** The document's expected HTTP status (default 200). */
  status?: number;
  gallery?: boolean;
  /** "document" or "frame": the planted overflow the self-test expects the run to catch. */
  selftest?: "document" | "frame";
}

export const PAGES: VisualPage[] = [
  { slug: "login", route: "/login", path: "/login", auth: "public", frame: false },
  { slug: "not-found", route: "(not-found)", path: "/this-page-does-not-exist", auth: "public", frame: false, status: 404 },
  { slug: "kit-gallery", route: "/kit-gallery", path: "/kit-gallery", auth: "public", gallery: true },
  { slug: "selftest-document", route: "/kit-gallery", path: "/kit-gallery?plant=overflow", auth: "public", gallery: true, selftest: "document" },
  { slug: "selftest-frame", route: "/kit-gallery", path: "/kit-gallery?plant=frame", auth: "public", gallery: true, selftest: "frame" },
  { slug: "dashboard", route: "/", path: "/", auth: "staff" },
  { slug: "audit-log", route: "/audit-log", path: "/audit-log", auth: "staff" },
  { slug: "b2b-desk", route: "/b2b-desk", path: "/b2b-desk", auth: "staff" },
  { slug: "billing", route: "/billing", path: "/billing", auth: "staff" },
  { slug: "invoice", route: "/billing/invoices/[number]", path: "/billing/invoices/INV-000004", auth: "staff" },
  { slug: "payment", route: "/billing/payments/[ref]", path: "/billing/payments/PMT-000007", auth: "staff" },
  { slug: "cases", route: "/cases", path: "/cases", auth: "staff" },
  { slug: "case", route: "/cases/[code]", path: "/cases/TC-000001", auth: "staff" },
  { slug: "countries", route: "/catalog/countries", path: "/catalog/countries", auth: "staff" },
  { slug: "country", route: "/catalog/countries/[iso]", path: "/catalog/countries/LA", auth: "staff" },
  { slug: "destinations", route: "/catalog/destinations", path: "/catalog/destinations", auth: "staff" },
  { slug: "trip-plans", route: "/catalog/trip-plans", path: "/catalog/trip-plans", auth: "staff" },
  { slug: "trip-plan", route: "/catalog/trip-plans/[sourceId]", path: "/catalog/trip-plans/tour-00001", auth: "staff" },
  { slug: "customers", route: "/customers", path: "/customers", auth: "staff" },
  { slug: "customer", route: "/customers/[id]", path: "/customers/CUS-000001", auth: "staff" },
  { slug: "hotels", route: "/hotels", path: "/hotels", auth: "staff" },
  { slug: "notifications", route: "/notifications", path: "/notifications", auth: "staff" },
  { slug: "pricing-rules", route: "/pricing/rules", path: "/pricing/rules", auth: "staff" },
  { slug: "agency-tiers", route: "/pricing/agency-tiers", path: "/pricing/agency-tiers", auth: "staff" },
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

/** Routes with a page.tsx that the run does not open, each with its reason. */
export const EXCLUDED: Record<string, string> = {
  "/cases/new": "a write form (new trip case): the layout-mock API answers reads only, so the form cannot load its pickers",
  "/cases/[code]/proposals/[n]": "a proposal version: no layout-mock fixture for the version endpoints yet",
  "/cases/[code]/proposals/[n]/preview": "the customer preview of a proposal version: no layout-mock fixture for it yet",
  "/catalog/trip-plans/[sourceId]/commercial": "the commercial editor of a trip plan (writes): no layout-mock fixture for it yet",
  "/pricing/calculator": "the quote calculator posts every change to the API (writes), which the layout mock does not answer",
  "/suppliers/new": "a write form (new supplier): the layout-mock API answers reads only",
  "/suppliers/[id]/edit": "a write form (supplier edit): the layout-mock API answers reads only",
  "/suppliers/rate-sheets/[id]": "a rate sheet import: no layout-mock fixture for rate sheets yet",
};
