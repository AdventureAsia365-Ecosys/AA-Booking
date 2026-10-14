"use client";
// ui/BottomNav.tsx
// AA-683 — adapted copy of AA-CIS-App frontend/app/_kit/BottomNav.tsx (CIS commit 039bb250): fixed phone
// nav (< 768 px, ui/shell.css). Booking items (UI spec §2.1): Dashboard, Cases, Customers, Notifications,
// More (opens the drawer with the full nav). Items the role cannot open are left out.

import { usePathname, useRouter } from "next/navigation";
import { Bell, Briefcase, LayoutGrid, Menu, Users } from "lucide-react";
import { K } from "./tokens";
import { navActive } from "./nav";
import { useStaff } from "./StaffContext";
import { canSeePage } from "@/lib/rbac";

const ITEMS = [
  { href: "/", label: "Dashboard", icon: LayoutGrid },
  { href: "/cases", label: "Cases", icon: Briefcase },
  { href: "/customers", label: "Customers", icon: Users },
  { href: "/notifications", label: "Alerts", icon: Bell },
] as const;

export function BottomNav({ onOpenMore }: { onOpenMore: () => void }) {
  const router = useRouter();
  const pathname = usePathname();
  const { role } = useStaff();

  return (
    <nav className="aa-bottom-nav" data-testid="admin-bottom-nav" aria-label="Primary (mobile)">
      {ITEMS.filter((it) => canSeePage(role, it.href)).map((it) => {
        const Icon = it.icon;
        const active = navActive({ href: it.href }, pathname);
        return (
          <button
            key={it.href}
            onClick={() => router.push(it.href)}
            aria-current={active ? "page" : undefined}
            aria-label={it.label}
            style={bnBtn(active)}
          >
            <Icon size={20} />
            <span style={{ fontSize: 10.5, fontWeight: active ? 600 : 500 }}>{it.label}</span>
          </button>
        );
      })}
      <button onClick={onOpenMore} aria-label="More navigation" style={bnBtn(false)}>
        <Menu size={20} />
        <span style={{ fontSize: 10.5, fontWeight: 500 }}>More</span>
      </button>
    </nav>
  );
}

function bnBtn(active: boolean): React.CSSProperties {
  return {
    flex: 1,
    minHeight: 44,
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    justifyContent: "center",
    gap: 2,
    border: "none",
    background: "transparent",
    cursor: "pointer",
    color: active ? K.accent : K.muted,
    padding: "6px 2px",
  };
}
