"use client";
// ui/local/nav.tsx (AA-994 S4b: moved from ui/, the Booking shell; not part of the kit core)
// AA-683 — the Booking navigation model (replaces CIS adminNav.tsx), shared by the Sidebar, the Topbar
// breadcrumb, the CommandPalette and the BottomNav. Order, groups, labels and icons are UI spec §2.2
// (Figma regrouped per shell C-1/C-10). An item shows only when the role may open its page
// (lib/rbac.ts) AND the entry itself is marked `shipped`: later-wave routes exist (spec §3) but stay
// out of the nav until their page is built. The flag is per entry (S237), so one page of a wave can ship
// without exposing the frames of the rest of its wave.

import {
  Activity,
  Award,
  Bell,
  Briefcase,
  Building,
  Building2,
  Calculator,
  CalendarDays,
  CalendarRange,
  ClipboardList,
  Coins,
  CreditCard,
  Gauge,
  Globe,
  Handshake,
  Landmark,
  LayoutGrid,
  Map as MapIcon,
  MapPin,
  Percent,
  PartyPopper,
  Scale,
  Settings,
  Shield,
  SquareCheck,
  Store,
  Sun,
  Wrench,
} from "lucide-react";
import { canSeePage } from "@/lib/rbac";
import { hrefPath, navActive, type NavMatch } from "./navMatch";

export type Wave = 1 | 2 | 3;

export interface NavEntry extends NavMatch {
  href: string;
  label: string;
  icon: React.ReactNode;
  /** The build wave of the page (UI spec §8); informational. */
  wave: Wave;
  /** The page reads the real API: show it in the nav. Unshipped routes still render their WaveFrame. */
  shipped: boolean;
  /** Other routes that belong to this entry (active-state matching). */
  also?: string[];
  /** AA-1037: an entry that opens one tab of a page (`href` carries `?tab=<tab>`) is active only on that tab. */
  tab?: string;
  /** AA-1037: the entry of the page itself is not active on these tabs (another entry stands for them). */
  notTabs?: string[];
}

export interface NavGroup {
  label: string;
  items: NavEntry[];
}

const ICON = 16;

// AA-1037 (root ADR 0019 d8, B2B-only model): partner agencies first. "Partners" is the Agencies tab of Agency billing
// (each agency's billing status), "Settlement" the rest of it (software fee and tour settlement invoices, payments).
const B2B_GROUP: NavGroup = {
  label: "B2B",
  items: [
    {
      href: "/agency-billing?tab=agencies",
      label: "Partners",
      icon: <Building2 size={ICON} />,
      wave: 3,
      shipped: true,
      tab: "agencies",
    },
    { href: "/agency-billing", label: "Settlement", icon: <Landmark size={ICON} />, wave: 3, shipped: true, notTabs: ["agencies"] },
    { href: "/b2b-desk", label: "B2B desk", icon: <Handshake size={ICON} />, wave: 3, shipped: true },
  ],
};

// AA-682 S4: AA's own payables. The customer invoices and payments that shared /billing are retired with the B2C half
// of Booking (AA-1046), and so are the Customers pages.
const SUPPLIER_BILLS: NavEntry = { href: "/billing", label: "Supplier bills", icon: <CreditCard size={ICON} />, wave: 3, shipped: true };

/** The nav groups (AA-1046: B2B only; the read-only group of retired traveller records went with the customer pages). */
export function navGroups(): NavGroup[] {
  return [
    {
      label: "Overview",
      items: [{ href: "/", label: "Dashboard", icon: <LayoutGrid size={ICON} />, wave: 1, shipped: true }],
    },
    B2B_GROUP,
    {
      label: "Sales",
      items: [
        { href: "/cases", label: "Trip Cases", icon: <Briefcase size={ICON} />, wave: 1, shipped: true },
        { href: "/proposals", label: "Proposals", icon: <MapIcon size={ICON} />, wave: 1, shipped: true },
        { href: "/pricing/calculator", label: "Pricing calculator", icon: <Calculator size={ICON} />, wave: 1, shipped: true },
      ],
    },
    {
      label: "Operations",
      items: [
        SUPPLIER_BILLS,
        // AA-1074 (bk-045): departure shared costs, purchase orders, profit per case / departure / agency.
        { href: "/finance", label: "Costs & profit", icon: <Coins size={ICON} />, wave: 3, shipped: true },
        // AA-1073 rest (bk-048): the departure board (month calendar + table across trip plans).
        { href: "/departures", label: "Departures", icon: <CalendarRange size={ICON} />, wave: 3, shipped: true },
        { href: "/trip-checks", label: "Trip Checks", icon: <SquareCheck size={ICON} />, wave: 3, shipped: true },
      ],
    },
    {
      label: "Supply",
      items: [
        { href: "/suppliers", label: "Suppliers", icon: <Store size={ICON} />, wave: 2, shipped: true },
        { href: "/hotels", label: "Hotels", icon: <Building size={ICON} />, wave: 2, shipped: true },
        { href: "/services", label: "Services", icon: <Activity size={ICON} />, wave: 2, shipped: true },
        { href: "/pricing/rules", label: "Pricing rules & FX", icon: <Percent size={ICON} />, wave: 2, shipped: true },
        { href: "/pricing/agency-tiers", label: "Agency tiers", icon: <Award size={ICON} />, wave: 2, shipped: true },
        // AA-1029: which trip plans price and why the others do not.
        { href: "/pricing/readiness", label: "Price readiness", icon: <Gauge size={ICON} />, wave: 2, shipped: true },
        // AA-1091: the price setup workbench (checks and next step per tour, suggested components, bulk workbook).
        { href: "/pricing/workbench", label: "Price setup", icon: <Wrench size={ICON} />, wave: 2, shipped: true },
        // AA-1091 (bk-050): the pricing policy parameters (quote validity, surcharges, deposits ...), maker-checker.
        { href: "/pricing/policy", label: "Pricing policy", icon: <Scale size={ICON} />, wave: 2, shipped: true },
        // AA-1091 (bk-054): season calendars and holiday events as data, maker-checker.
        { href: "/pricing/seasons", label: "Seasons", icon: <Sun size={ICON} />, wave: 2, shipped: true },
        { href: "/pricing/holiday-events", label: "Holiday events", icon: <PartyPopper size={ICON} />, wave: 2, shipped: true },
      ],
    },
    {
      label: "Catalog (from CIS)",
      items: [
        { href: "/catalog/trip-plans", label: "Trip Plans", icon: <CalendarDays size={ICON} />, wave: 2, shipped: true },
        { href: "/catalog/destinations", label: "Destinations", icon: <MapPin size={ICON} />, wave: 2, shipped: true },
        { href: "/catalog/countries", label: "Countries", icon: <Globe size={ICON} />, wave: 2, shipped: true },
      ],
    },
    {
      label: "Admin",
      items: [
        { href: "/staff", label: "Staff", icon: <Shield size={ICON} />, wave: 1, shipped: true },
        { href: "/audit-log", label: "Audit Log", icon: <ClipboardList size={ICON} />, wave: 2, shipped: true },
      ],
    },
  ];
}

export const NAV_GROUPS: NavGroup[] = navGroups();

/** Footer / avatar-menu entry (all roles). */
export const SETTINGS_ENTRY: NavEntry = {
  href: "/settings",
  label: "Settings",
  icon: <Settings size={ICON} />,
  wave: 1,
  shipped: true,
};

/** Topbar bell target (all roles). */
export const NOTIFICATIONS_ENTRY: NavEntry = {
  href: "/notifications",
  label: "Notifications",
  icon: <Bell size={ICON} />,
  wave: 1,
  shipped: true,
};

/** Every route (all waves), used for titles even when the nav hides the item. */
const ALL_ENTRIES: NavEntry[] = [...NAV_GROUPS.flatMap((g) => g.items), SETTINGS_ENTRY, NOTIFICATIONS_ENTRY];

export { hrefPath, navActive } from "./navMatch";

export function isVisible(entry: NavEntry, role: string | null | undefined): boolean {
  return entry.shipped && canSeePage(role, hrefPath(entry.href));
}

/** Nav groups filtered for a role (empty groups dropped). */
export function visibleGroups(role: string | null | undefined): NavGroup[] {
  return NAV_GROUPS.map((g) => ({ ...g, items: g.items.filter((i) => isVisible(i, role)) })).filter(
    (g) => g.items.length > 0,
  );
}

/** Flat list of visible entries plus Notifications and Settings (command palette). */
export function allEntries(role: string | null | undefined): NavEntry[] {
  return [...visibleGroups(role).flatMap((g) => g.items), NOTIFICATIONS_ENTRY, SETTINGS_ENTRY].filter((e) =>
    canSeePage(role, hrefPath(e.href)),
  );
}

/** The breadcrumb title for a path: the longest matching entry, or a prettified first segment. */
export function pageTitle(pathname: string): string {
  let best: NavEntry | null = null;
  for (const e of ALL_ENTRIES) {
    if (navActive(e, pathname) && (!best || e.href.length > best.href.length)) best = e;
  }
  if (best) return best.label;
  const slug = pathname.split("/").filter(Boolean)[0] ?? "";
  return slug ? slug.replace(/-/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()) : "Dashboard";
}
