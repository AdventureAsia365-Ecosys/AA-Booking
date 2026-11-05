// ui/local/navMatch.ts — the active-state rule of the Booking nav (ui/local/nav.tsx), pure so that
// ui/booking/b2cFrozen.test.mjs can test it with node --test (nav.tsx itself holds JSX icons).
// AA-1037: an entry may open one tab of a page (`/agency-billing?tab=agencies`): `tab` makes it active on that tab
// only, `notTabs` keeps the page's own entry off those tabs.

export interface NavMatch {
  href: string;
  /** Other routes that belong to this entry (active-state matching). */
  also?: string[];
  /** The entry opens this tab of its page (`href` carries `?tab=<tab>`): active on that tab only. */
  tab?: string;
  /** The page's own entry is not active on these tabs (another entry stands for them). */
  notTabs?: string[];
}

/** The path of an entry's href. */
export function hrefPath(href: string): string {
  return href.split("?")[0] || "/";
}

/** Is the entry the current page? `tab` is the page's `tab` URL key (null when absent). */
export function navActive(entry: NavMatch, pathname: string, tab: string | null = null): boolean {
  const all = [hrefPath(entry.href), ...(entry.also ?? [])];
  const onPage = all.some((h) => (h === "/" ? pathname === "/" : pathname === h || pathname.startsWith(h + "/")));
  if (!onPage) return false;
  if (entry.tab !== undefined) return pathname === hrefPath(entry.href) && tab === entry.tab;
  return !(tab !== null && pathname === hrefPath(entry.href) && entry.notTabs?.includes(tab));
}
