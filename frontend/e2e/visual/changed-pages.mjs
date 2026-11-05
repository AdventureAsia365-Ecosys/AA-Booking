// frontend/e2e/visual/changed-pages.mjs — AA-1008 (root ADR 0016 decision 9): which pages of pages.ts a change touches,
// so the visual run captures "every changed page". Prints one line: `all`, a comma list of slugs, or nothing.
//   node --experimental-strip-types e2e/visual/changed-pages.mjs <base> <head>     (from frontend/)
//   node --experimental-strip-types e2e/visual/changed-pages.mjs --files <path>...  (the unit test)
// Rules (paths from the repo root):
//   - a file under frontend/app/: the page folders at or under its folder, cut at the first private folder (`_x`)
//     and without route groups (`(x)`): app/(staff)/cases/_parts/X.tsx touches every page under app/cases;
//     app/(staff)/_parts/**, app/layout.tsx and app/globals.css touch every page;
//   - any other file of frontend/ (ui, lib, proxy.ts, package.json, the layout-mock fixtures…), the visual run's
//     own files and its workflow: every page;
//   - anything else (backend/, migrations/, docs/…): no page.
import { execFileSync } from "node:child_process";
import { PAGES } from "./pages.ts";

const APP = "frontend/app/";
const EVERYTHING = ["frontend/", ".github/workflows/visual.yml"];

/** The folder of a route under app/ ("/admin/tenants" -> "admin/tenants"); null for a page without a page.tsx. */
function routeFolder(route) {
  if (!route.startsWith("/")) return null; // "(not-found)"
  return route === "/" ? "" : route.slice(1);
}

/** The slugs a list of changed paths touches: "all", or a sorted list (maybe empty). */
export function changedSlugs(files, pages = PAGES) {
  const slugs = new Set();
  for (const f of files) {
    if (f.startsWith(APP)) {
      const segs = f.slice(APP.length).split("/").slice(0, -1);
      // A private folder (`_x`) and everything under it belong to the folder that holds it.
      const priv = segs.findIndex((s) => s.startsWith("_"));
      if (priv >= 0) segs.length = priv;
      const folder = segs.filter((s) => !/^\(.*\)$/.test(s)).join("/");
      if (folder === "") return "all";
      for (const p of pages) {
        const rf = routeFolder(p.route);
        if (rf !== null && (rf === folder || rf.startsWith(`${folder}/`))) slugs.add(p.slug);
      }
      continue;
    }
    if (EVERYTHING.some((e) => f === e || f.startsWith(e))) return "all";
  }
  return [...slugs].sort();
}

const isMain = process.argv[1] && import.meta.url.endsWith(process.argv[1].split("/").pop());
if (isMain) {
  const args = process.argv.slice(2);
  let files;
  if (args[0] === "--files") files = args.slice(1);
  else {
    const [base, head] = args;
    if (!base || !head) {
      console.error("usage: changed-pages.mjs <base> <head> | --files <path>...");
      process.exit(2);
    }
    files = execFileSync("git", ["diff", "--name-only", `${base}...${head}`], { encoding: "utf8", cwd: new URL("../../..", import.meta.url) }).split("\n").filter(Boolean);
  }
  const out = changedSlugs(files);
  console.log(out === "all" ? "all" : out.join(","));
}
