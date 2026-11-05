"use client";
// ui/local/BottomNav.tsx (AA-994 S4b: moved from ui/, the Booking shell; not part of the kit core)
// AA-683 — adapted copy of AA-CIS-App frontend/app/_kit/BottomNav.tsx (CIS commit 039bb250): fixed phone
// nav (< 768 px, ui/local/shell.css). Booking items (UI spec §2.1): Dashboard, Cases, Customers, Notifications,
// More (opens the drawer with the full nav). Items the role cannot open are left out.
// AA-1037 (root ADR 0019 d8): with the B2C part closed (NEXT_PUBLIC_B2C_INTAKE unset, the default) the third item is
// the B2B desk instead of Customers (frozen B2C records, still under More).

import { usePathname, useRouter } from "next/navigation";
import { Bell, Briefcase, Handshake, LayoutGrid, Menu, Users } from "lucide-react";
import { B2C_INTAKE_OPEN } from "@/lib/config";
import { K } from "../core/tokens";
import { navActive } from "./nav";
import { useStaff } from "./StaffContext";
import { canSeePage } from "@/lib/rbac";

const ITEMS = [
  { href: "/", label: "Dashboard", icon: LayoutGrid },
  { href: "/cases", label: "Cases", icon: Briefcase },
  B2C_INTAKE_OPEN
    ? { href: "/customers", label: "Customers", icon: Users }
    : { href: "/b2b-desk", label: "B2B desk", icon: Handshake },
  { href: "/notifications", label: "Alerts", icon: Bell },
];

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
