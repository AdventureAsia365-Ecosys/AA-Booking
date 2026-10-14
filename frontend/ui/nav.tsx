"use client";
// ui/nav.tsx
// AA-683 — the Booking navigation model (replaces CIS adminNav.tsx), shared by the Sidebar, the Topbar
// breadcrumb, the CommandPalette and the BottomNav. Order, groups, labels and icons are UI spec §2.2
// (Figma regrouped per shell C-1/C-10). An item shows only when the role may open its page
// (lib/rbac.ts) AND its wave has shipped (SHIPPED_WAVE): later-wave routes exist (spec §3) but stay
// out of the nav until their wave ships.

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

/** Raise when a wave ships: its nav items appear (Nghiệp S236 decision 3, all 3 waves are built). */
export const SHIPPED_WAVE: Wave = 1;

export interface NavEntry {
  href: string;
  label: string;
  icon: React.ReactNode;
  wave: Wave;
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
    items: [{ href: "/", label: "Dashboard", icon: <LayoutGrid size={ICON} />, wave: 1 }],
  },
  {
    label: "Sales",
    items: [
      { href: "/cases", label: "Trip Cases", icon: <Briefcase size={ICON} />, wave: 1 },
      { href: "/proposals", label: "Proposals", icon: <MapIcon size={ICON} />, wave: 1 },
      { href: "/customers", label: "Customers", icon: <Users size={ICON} />, wave: 1 },
      { href: "/pricing/calculator", label: "Pricing calculator", icon: <Calculator size={ICON} />, wave: 1 },
    ],
  },
  {
    label: "Operations",
    items: [
      { href: "/billing", label: "Payments & Invoices", icon: <CreditCard size={ICON} />, wave: 3 },
      { href: "/trip-checks", label: "Trip Checks", icon: <SquareCheck size={ICON} />, wave: 3 },
    ],
  },
  {
    label: "Supply",
    items: [
      { href: "/suppliers", label: "Suppliers", icon: <Store size={ICON} />, wave: 2 },
      { href: "/hotels", label: "Hotels", icon: <Building size={ICON} />, wave: 2 },
      { href: "/services", label: "Services", icon: <Activity size={ICON} />, wave: 2 },
      { href: "/pricing/rules", label: "Pricing rules & FX", icon: <Percent size={ICON} />, wave: 2 },
    ],
  },
  {
    label: "Catalog (from CIS)",
    items: [
      { href: "/catalog/trip-plans", label: "Trip Plans", icon: <CalendarDays size={ICON} />, wave: 2 },
      { href: "/catalog/destinations", label: "Destinations", icon: <MapPin size={ICON} />, wave: 2 },
      { href: "/catalog/countries", label: "Countries", icon: <Globe size={ICON} />, wave: 2 },
    ],
  },
  {
    label: "Admin",
    items: [
      { href: "/staff", label: "Staff", icon: <Shield size={ICON} />, wave: 1 },
      { href: "/audit-log", label: "Audit Log", icon: <ClipboardList size={ICON} />, wave: 2 },
    ],
  },
];

/** Footer / avatar-menu entry (all roles). */
export const SETTINGS_ENTRY: NavEntry = {
  href: "/settings",
  label: "Settings",
  icon: <Settings size={ICON} />,
  wave: 1,
};

/** Topbar bell target (all roles). */
export const NOTIFICATIONS_ENTRY: NavEntry = {
  href: "/notifications",
  label: "Notifications",
  icon: <Bell size={ICON} />,
  wave: 1,
};

/** Every route (all waves), used for titles even when the nav hides the item. */
const ALL_ENTRIES: NavEntry[] = [...NAV_GROUPS.flatMap((g) => g.items), SETTINGS_ENTRY, NOTIFICATIONS_ENTRY];

export function navActive(entry: Pick<NavEntry, "href" | "also">, pathname: string): boolean {
  const all = [entry.href, ...(entry.also ?? [])];
  return all.some((h) => (h === "/" ? pathname === "/" : pathname === h || pathname.startsWith(h + "/")));
}

export function isVisible(entry: NavEntry, role: string | null | undefined): boolean {
  return entry.wave <= SHIPPED_WAVE && canSeePage(role, entry.href);
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
