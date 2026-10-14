"use client";
// ui/Sidebar.tsx
// AA-683 — adapted copy of AA-CIS-App frontend/app/_kit/Sidebar.tsx (CIS commit 039bb250). Same
// structure, CSS classes (ui/shell.css) and collapse behaviour (chevron + `[` key, 68 px icon rail,
// phone drawer). Booking changes (UI spec §2.1–2.2, 01-shell §0.5):
//   - nav model from ui/nav.tsx (role- and wave-filtered), logo label "Booking";
//   - active item = `--aa-side-panel` pill with gold icon + text (Figma; spec §1.2 row "Sidebar active pill");
//   - footer = staff card (initials, name, Booking role) with a sign-out button, then Settings + collapse.

import { useEffect } from "react";
import { usePathname, useRouter } from "next/navigation";
import { ChevronsLeft, ChevronsRight, LogOut } from "lucide-react";
import { K, alpha, sans, serif } from "./tokens";
import { LOGO_SRC } from "./brand";
import { SETTINGS_ENTRY, navActive, visibleGroups, type NavEntry } from "./nav";
import { useSidebarCollapsed } from "./useSidebar";
import { staffDisplay, useStaff } from "./StaffContext";
import { signOut } from "./signOut";

export function Sidebar({ onNavigate, drawerOpen = false }: { onNavigate?: () => void; drawerOpen?: boolean }) {
  const router = useRouter();
  const pathname = usePathname();
  const staff = useStaff();
  const { name, initials, roleLabel } = staffDisplay(staff);
  const [collapsed, setCollapsed] = useSidebarCollapsed();
  const groups = visibleGroups(staff.role);

  // `[` toggles collapse, ignored while typing.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "[" || e.metaKey || e.ctrlKey || e.altKey) return;
      const t = e.target as HTMLElement | null;
      const tag = t?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || t?.isContentEditable) return;
      e.preventDefault();
      setCollapsed(!collapsed);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [collapsed, setCollapsed]);

  function go(href: string) {
    router.push(href);
    onNavigate?.();
  }

  return (
    <aside
      className={`aa-admin-sidebar aa-shell-sidebar${drawerOpen ? " aa-admin-sidebar--open" : ""}`}
      data-testid="admin-sidebar"
      data-collapsed={collapsed ? "true" : "false"}
      style={{
        width: collapsed ? 68 : 248,
        flexShrink: 0,
        background: "var(--aa-side-bg)",
        color: "var(--aa-side-text)",
        padding: "22px 14px 18px",
        display: "flex",
        flexDirection: "column",
        gap: 22,
        height: "100vh",
        overflowY: "auto",
        overflowX: "hidden",
        transition: "width .18s ease",
      }}
    >
      {/* Brand → Dashboard */}
      <div style={{ paddingBottom: 16, borderBottom: "1px solid rgba(255,255,255,0.07)" }}>
        <button
          onClick={() => go("/")}
          data-testid="sidebar-logo"
          title="Adventure Asia Booking: Dashboard"
          aria-label="Adventure Asia Booking, go to Dashboard"
          style={{
            display: "flex",
            alignItems: "center",
            gap: 10,
            width: "100%",
            background: "none",
            border: "none",
            cursor: "pointer",
            padding: 0,
          }}
        >
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={LOGO_SRC}
            alt="Adventure Asia"
            width={38}
            height={24}
            style={{ width: 38, height: "auto", flexShrink: 0, display: "block" }}
          />
          <span className="aa-sidebar-label" style={{ flex: 1, minWidth: 0, textAlign: "left" }}>
            <span
              style={{
                display: "block",
                fontFamily: serif,
                fontSize: 14,
                fontWeight: 500,
                color: "var(--aa-side-title)",
                letterSpacing: "-0.01em",
                lineHeight: 1.2,
              }}
            >
              Adventure Asia
            </span>
            <span
              style={{
                display: "block",
                fontSize: 9.5,
                textTransform: "uppercase",
                letterSpacing: "0.18em",
                color: K.accent,
                fontWeight: 600,
                marginTop: 1,
              }}
            >
              Booking
            </span>
          </span>
        </button>
      </div>

      {/* Nav */}
      <nav aria-label="Primary" style={{ flex: 1, display: "flex", flexDirection: "column", gap: 18 }}>
        {groups.map((group) => (
          <div key={group.label}>
            <div
              className="aa-sidebar-group-title"
              style={{
                fontSize: 9.5,
                textTransform: "uppercase",
                letterSpacing: "0.16em",
                color: "var(--aa-side-text3)",
                padding: "0 10px 8px",
                fontWeight: 600,
              }}
            >
              {group.label}
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
              {group.items.map((n) => (
                <NavItem key={n.href} entry={n} active={navActive(n, pathname)} onClick={() => go(n.href)} />
              ))}
            </div>
          </div>
        ))}
      </nav>

      {/* Settings, staff card, collapse */}
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <NavItem
          entry={SETTINGS_ENTRY}
          active={navActive(SETTINGS_ENTRY, pathname)}
          onClick={() => go(SETTINGS_ENTRY.href)}
        />
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 10,
            padding: "10px 8px 4px",
            borderTop: "1px solid rgba(255,255,255,0.07)",
          }}
        >
          <span
            aria-hidden="true"
            title={`${name} (${roleLabel})`}
            style={{
              width: 32,
              height: 32,
              borderRadius: "50%",
              flexShrink: 0,
              display: "grid",
              placeItems: "center",
              background: alpha(K.accent, 22),
              color: K.accent,
              fontSize: 12,
              fontWeight: 700,
              fontFamily: sans,
            }}
          >
            {initials}
          </span>
          <span className="aa-sidebar-label" style={{ flex: 1, minWidth: 0 }}>
            <span
              style={{
                display: "block",
                fontSize: 12.5,
                fontWeight: 600,
                color: "var(--aa-side-title)",
                whiteSpace: "nowrap",
                overflow: "hidden",
                textOverflow: "ellipsis",
              }}
            >
              {name}
            </span>
            <span style={{ display: "block", fontSize: 11, color: "var(--aa-side-text2)" }}>{roleLabel}</span>
          </span>
          <button
            className="aa-sidebar-label"
            onClick={() => signOut(router)}
            title="Sign out"
            aria-label="Sign out"
            style={{
              display: "grid",
              placeItems: "center",
              width: 30,
              height: 30,
              borderRadius: 8,
              border: "1px solid rgba(255,255,255,0.10)",
              background: "transparent",
              color: "var(--aa-side-text2)",
              cursor: "pointer",
              flexShrink: 0,
            }}
          >
            <LogOut size={14} />
          </button>
        </div>
        <button
          onClick={() => setCollapsed(!collapsed)}
          data-testid="sidebar-collapse"
          title={collapsed ? "Expand sidebar ([)" : "Collapse sidebar ([)"}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 10,
            width: "100%",
            padding: "8px 10px",
            borderRadius: 7,
            border: "none",
            background: "transparent",
            color: "var(--aa-side-text2)",
            cursor: "pointer",
            fontSize: 12,
            fontFamily: sans,
          }}
        >
          {collapsed ? <ChevronsRight size={16} /> : <ChevronsLeft size={16} />}
          <span className="aa-sidebar-label">Collapse</span>
        </button>
      </div>
    </aside>
  );
}

function NavItem({ entry, active, onClick }: { entry: NavEntry; active: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      aria-current={active ? "page" : undefined}
      title={entry.label}
      aria-label={entry.label}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 10,
        width: "100%",
        padding: "8px 10px",
        borderRadius: 10,
        border: active ? "1px solid rgba(255,255,255,0.06)" : "1px solid transparent",
        background: active ? "var(--aa-side-panel)" : "transparent",
        color: active ? K.accent : "var(--aa-side-text)",
        fontSize: 13,
        fontWeight: active ? 600 : 500,
        cursor: "pointer",
        textAlign: "left",
        fontFamily: sans,
        transition: "background .15s, color .15s",
      }}
    >
      <span style={{ flexShrink: 0, opacity: active ? 1 : 0.8, display: "flex" }}>{entry.icon}</span>
      <span
        className="aa-sidebar-label"
        style={{ flex: 1, minWidth: 0, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}
      >
        {entry.label}
      </span>
    </button>
  );
}
