"use client";
// ui/Topbar.tsx
// AA-683 — adapted copy of AA-CIS-App frontend/app/_kit/Topbar.tsx (CIS commit 039bb250). Same 60 px bar,
// CSS classes (ui/shell.css) and phone element swap. Booking changes (UI spec §2.1, 01-shell §0.5):
//   - breadcrumb "Booking / <page>" from ui/nav.tsx;
//   - right: theme switch, then the Figma pill cluster: bell (unread dot → popover) | dashed divider |
//     role label + avatar → menu (Settings, Open CIS admin ↗, Sign out).

import { useEffect, useRef, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { ChevronDown, ExternalLink, LogOut, Menu as MenuIcon, Monitor, Moon, Search, Settings, Sun } from "lucide-react";
import { K, sans, serif } from "./tokens";
import { useThemeChoice } from "./useTheme";
import type { ThemeChoice } from "./theme";
import { pageTitle } from "./nav";
import { staffDisplay, useStaff } from "./StaffContext";
import { NotificationPopover } from "./booking/NotificationPopover";
import { signOut } from "./signOut";
import { CIS_ADMIN_URL } from "@/lib/config";

export function Topbar({ onOpenSearch, onOpenMenu }: { onOpenSearch: () => void; onOpenMenu: () => void }) {
  const pathname = usePathname();
  const title = pageTitle(pathname);
  const isMac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);

  return (
    <header
      className="aa-topbar"
      style={{
        height: 60,
        flexShrink: 0,
        background: K.card,
        borderBottom: `1px solid ${K.line}`,
        display: "flex",
        alignItems: "center",
        gap: 16,
        padding: "0 24px",
        position: "sticky",
        top: 0,
        zIndex: 20,
        fontFamily: sans,
        minWidth: 0,
      }}
    >
      <button
        className="aa-topbar-menu"
        onClick={onOpenMenu}
        aria-label="Open navigation menu"
        style={{
          display: "none",
          alignItems: "center",
          justifyContent: "center",
          width: 40,
          height: 40,
          borderRadius: 8,
          border: "none",
          background: "transparent",
          color: K.ink3,
          cursor: "pointer",
          flexShrink: 0,
        }}
      >
        <MenuIcon size={20} />
      </button>

      <div
        className="aa-topbar-crumb"
        style={{ display: "flex", alignItems: "center", gap: 6, minWidth: 0, flexShrink: 1, overflow: "hidden" }}
      >
        <span className="aa-topbar-crumb-root" style={{ fontSize: 13, color: K.muted2, flexShrink: 0 }}>
          Booking
        </span>
        <span className="aa-topbar-crumb-root" style={{ fontSize: 13, color: K.muted2, flexShrink: 0 }}>
          /
        </span>
        <span
          style={{
            fontSize: 13,
            fontWeight: 600,
            color: K.ink,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {title}
        </span>
      </div>

      <button
        className="aa-topbar-search"
        onClick={onOpenSearch}
        aria-label="Open search"
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          maxWidth: 420,
          flex: 1,
          minWidth: 0,
          margin: "0 auto",
          padding: "7px 12px",
          borderRadius: 999,
          border: `1px solid ${K.line}`,
          background: K.bg,
          color: K.muted,
          cursor: "pointer",
          fontFamily: sans,
          fontSize: 13,
        }}
      >
        <Search size={15} style={{ flexShrink: 0 }} />
        <span style={{ flex: 1, textAlign: "left", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          Search pages, trip cases, customers…
        </span>
        <kbd
          style={{
            fontSize: 11,
            color: K.muted,
            border: `1px solid ${K.line}`,
            borderRadius: 6,
            padding: "1px 6px",
            background: K.card,
            flexShrink: 0,
          }}
        >
          {isMac ? "⌘K" : "Ctrl K"}
        </kbd>
      </button>

      <span className="aa-topbar-spacer" style={{ display: "none", flex: 1 }} />

      <button
        className="aa-topbar-search-icon"
        onClick={onOpenSearch}
        aria-label="Search"
        style={{
          display: "none",
          alignItems: "center",
          justifyContent: "center",
          width: 40,
          height: 40,
          borderRadius: 8,
          border: "none",
          background: "transparent",
          color: K.ink3,
          cursor: "pointer",
          flexShrink: 0,
        }}
      >
        <Search size={18} />
      </button>

      <div style={{ display: "flex", alignItems: "center", gap: 8, flexShrink: 0 }}>
        <span className="aa-topbar-theme">
          <ThemeToggle />
        </span>
        {/* Figma pill cluster: bell | dashed divider | role + avatar */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: 3,
            border: `1px solid ${K.line}`,
            borderRadius: 12,
            background: K.card,
          }}
        >
          <NotificationPopover />
          <span aria-hidden="true" style={{ width: 0, height: 24, borderLeft: `1px dashed ${K.line}` }} />
          <AccountMenu />
        </div>
      </div>
    </header>
  );
}

const THEME_OPTIONS: { key: ThemeChoice; label: string; icon: React.ReactNode }[] = [
  { key: "light", label: "Light", icon: <Sun size={16} /> },
  { key: "dark", label: "Dark", icon: <Moon size={16} /> },
  { key: "system", label: "System", icon: <Monitor size={16} /> },
];

function ThemeToggle() {
  const [choice, setChoice] = useThemeChoice();
  const i = Math.max(
    0,
    THEME_OPTIONS.findIndex((o) => o.key === choice),
  );
  const cur = THEME_OPTIONS[i];
  const next = THEME_OPTIONS[(i + 1) % THEME_OPTIONS.length];
  return (
    <button
      onClick={() => setChoice(next.key)}
      title={`Theme: ${cur.label}. Click for ${next.label}`}
      aria-label={`Theme: ${cur.label}. Switch to ${next.label}`}
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        width: 36,
        height: 36,
        borderRadius: 8,
        border: "none",
        background: "transparent",
        cursor: "pointer",
        color: choice === "system" ? K.muted : K.accent,
      }}
    >
      {cur.icon}
    </button>
  );
}

function AccountMenu() {
  const router = useRouter();
  const staff = useStaff();
  const { name, initials, roleLabel } = staffDisplay(staff);
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div ref={ref} style={{ position: "relative" }}>
      <button
        onClick={() => setOpen((v) => !v)}
        aria-label="Account menu"
        aria-expanded={open}
        aria-haspopup="menu"
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          padding: "2px 6px 2px 2px",
          borderRadius: 10,
          border: "none",
          background: "transparent",
          cursor: "pointer",
          fontFamily: sans,
        }}
      >
        <span
          className="aa-topbar-account-name"
          style={{ fontSize: 12.5, fontWeight: 600, color: K.ink3, paddingLeft: 6, whiteSpace: "nowrap" }}
        >
          {roleLabel}
        </span>
        <span
          style={{
            width: 30,
            height: 30,
            borderRadius: "50%",
            background: K.accent,
            display: "grid",
            placeItems: "center",
            color: "var(--aa-on-accent)",
            fontWeight: 700,
            fontSize: 11.5,
          }}
        >
          {initials}
        </span>
        <ChevronDown size={14} style={{ color: K.muted2 }} />
      </button>
      {open && (
        <div
          role="menu"
          style={{
            position: "absolute",
            top: "100%",
            right: 0,
            marginTop: 8,
            width: 230,
            zIndex: 100,
            background: K.card,
            border: `1px solid ${K.line}`,
            borderRadius: 10,
            boxShadow: "0 12px 32px rgba(0,0,0,0.18)",
            overflow: "hidden",
          }}
        >
          <div style={{ padding: "10px 14px", borderBottom: `1px solid ${K.line}` }}>
            <div
              style={{
                fontSize: 13,
                fontWeight: 600,
                color: K.ink,
                fontFamily: serif,
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
            >
              {name}
            </div>
            <div style={{ fontSize: 11, color: K.muted }}>
              {roleLabel}
              {staff.email ? ` · ${staff.email}` : ""}
            </div>
          </div>
          <MenuRow
            icon={<Settings size={15} />}
            label="Settings"
            onClick={() => {
              setOpen(false);
              router.push("/settings");
            }}
          />
          <a
            role="menuitem"
            href={CIS_ADMIN_URL}
            target="_blank"
            rel="noopener noreferrer"
            style={{ ...menuRowStyle, textDecoration: "none" }}
          >
            <span style={{ color: K.muted2, display: "flex" }}>
              <ExternalLink size={15} />
            </span>
            Open CIS admin
          </a>
          <MenuRow icon={<LogOut size={15} />} label="Sign out" onClick={() => signOut(router)} />
        </div>
      )}
    </div>
  );
}

const menuRowStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 10,
  width: "100%",
  padding: "9px 14px",
  border: "none",
  background: "transparent",
  cursor: "pointer",
  color: K.ink3,
  fontSize: 13,
  fontFamily: sans,
  textAlign: "left",
};

function MenuRow({ icon, label, onClick }: { icon: React.ReactNode; label: string; onClick: () => void }) {
  return (
    <button role="menuitem" onClick={onClick} style={menuRowStyle}>
      <span style={{ color: K.muted2, display: "flex" }}>{icon}</span>
      {label}
    </button>
  );
}
