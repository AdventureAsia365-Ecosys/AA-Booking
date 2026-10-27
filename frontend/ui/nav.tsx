"use client";
// ui/nav.tsx
// AA-683 — the Booking navigation model (replaces CIS adminNav.tsx), shared by the Sidebar, the Topbar
// breadcrumb, the CommandPalette and the BottomNav. Order, groups, labels and icons are UI spec §2.2
// (Figma regrouped per shell C-1/C-10). An item shows only when the role may open its page
// (lib/rbac.ts) AND the entry itself is marked `shipped`: later-wave routes exist (spec §3) but stay
// out of the nav until their page is built. The flag is per entry (S237), so one page of a wave can ship
// without exposing the frames of the rest of its wave.

import {
  Activity,
  Bell,
  Briefcase,
  Building,
  Calculator,
  CalendarDays,
  ClipboardList,
  CreditCard,
  Globe,
  Handshake,
  LayoutGrid,
  Map as MapIcon,
  MapPin,
  Percent,
  Settings,
  Shield,
  SquareCheck,
  Store,
  Users,
} from "lucide-react";
import { canSeePage } from "@/lib/rbac";

export type Wave = 1 | 2 | 3;

export interface NavEntry {
  href: string;
  label: string;
  icon: React.ReactNode;
  /** The build wave of the page (UI spec §8); informational. */
  wave: Wave;
  /** The page reads the real API: show it in the nav. Unshipped routes still render their WaveFrame. */
  shipped: boolean;
  /** Other routes that belong to this entry (active-state matching). */
  also?: string[];
}

export interface NavGroup {
  label: string;
  items: NavEntry[];
}

const ICON = 16;

export const NAV_GROUPS: NavGroup[] = [
  {
    label: "Overview",
    items: [{ href: "/", label: "Dashboard", icon: <LayoutGrid size={ICON} />, wave: 1, shipped: true }],
  },
  {
    label: "Sales",
    items: [
      { href: "/cases", label: "Trip Cases", icon: <Briefcase size={ICON} />, wave: 1, shipped: true },
      { href: "/proposals", label: "Proposals", icon: <MapIcon size={ICON} />, wave: 1, shipped: true },
      { href: "/customers", label: "Customers", icon: <Users size={ICON} />, wave: 1, shipped: true },
      { href: "/pricing/calculator", label: "Pricing calculator", icon: <Calculator size={ICON} />, wave: 1, shipped: true },
    ],
  },
  {
    label: "Operations",
    items: [
      { href: "/billing", label: "Payments & Invoices", icon: <CreditCard size={ICON} />, wave: 3, shipped: false },
      { href: "/trip-checks", label: "Trip Checks", icon: <SquareCheck size={ICON} />, wave: 3, shipped: true },
      { href: "/b2b-desk", label: "B2B desk", icon: <Handshake size={ICON} />, wave: 3, shipped: true },
    ],
  },
  {
    label: "Supply",
    items: [
      { href: "/suppliers", label: "Suppliers", icon: <Store size={ICON} />, wave: 2, shipped: true },
      { href: "/hotels", label: "Hotels", icon: <Building size={ICON} />, wave: 2, shipped: true },
      { href: "/services", label: "Services", icon: <Activity size={ICON} />, wave: 2, shipped: true },
      { href: "/pricing/rules", label: "Pricing rules & FX", icon: <Percent size={ICON} />, wave: 2, shipped: true },
    ],
  },
  {
    label: "Catalog (from CIS)",
    items: [
      { href: "/catalog/trip-plans", label: "Trip Plans", icon: <CalendarDays size={ICON} />, wave: 2, shipped: true },
      { href: "/catalog/destinations", label: "Destinations", icon: <MapPin size={ICON} />, wave: 2, shipped: false },
      { href: "/catalog/countries", label: "Countries", icon: <Globe size={ICON} />, wave: 2, shipped: false },
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

export function navActive(entry: Pick<NavEntry, "href" | "also">, pathname: string): boolean {
  const all = [entry.href, ...(entry.also ?? [])];
  return all.some((h) => (h === "/" ? pathname === "/" : pathname === h || pathname.startsWith(h + "/")));
}

export function isVisible(entry: NavEntry, role: string | null | undefined): boolean {
  return entry.shipped && canSeePage(role, entry.href);
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
    canSeePage(role, e.href),
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
