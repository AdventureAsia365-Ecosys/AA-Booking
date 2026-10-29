"use client";
// ui/local/AdminShell.tsx (AA-994 S4b: moved from ui/, the Booking shell; not part of the kit core)
// AA-683 — adapted copy of AA-CIS-App frontend/app/_kit/AdminShell.tsx (CIS commit 039bb250): Sidebar |
// (Topbar over the page), ⌘K palette, phone drawer + BottomNav. Booking addition: the page-level role
// check (lib/rbac.ts). A page the signed-in role may not open renders a "no access" state instead of
// its content, so it never fires API calls that would only answer 403.

import { useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import Link from "next/link";
import { ShieldOff } from "lucide-react";
import { Sidebar } from "./Sidebar";
import { Topbar } from "./Topbar";
import { BottomNav } from "./BottomNav";
import { CommandPalette } from "./CommandPalette";
import { EmptyState } from "../core/primitives";
import { K } from "../core/tokens";
import { useStaff } from "./StaffContext";
import { canSeePage } from "@/lib/rbac";

export function AdminShell({ children }: { children: React.ReactNode }) {
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const pathname = usePathname();
  const { role } = useStaff();
  const allowed = canSeePage(role, pathname);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
        e.preventDefault();
        setPaletteOpen((v) => !v);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    if (!drawerOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setDrawerOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [drawerOpen]);

  return (
    <div style={{ display: "flex", minHeight: "100vh", background: K.bg }}>
      <Sidebar drawerOpen={drawerOpen} onNavigate={() => setDrawerOpen(false)} />
      {drawerOpen && <div className="aa-admin-backdrop" onClick={() => setDrawerOpen(false)} aria-hidden="true" />}
      <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", height: "100vh" }}>
        <Topbar onOpenSearch={() => setPaletteOpen(true)} onOpenMenu={() => setDrawerOpen(true)} />
        {allowed ? (
          children
        ) : (
          <main className="aa-admin-main flex-1 overflow-y-auto px-8 py-6">
            <EmptyState
              icon={<ShieldOff size={30} />}
              title="You do not have access to this page"
              description="Your Booking role does not include this page. Ask a Booking admin if you need it."
              action={
                <Link href="/" className="text-[13px] font-semibold text-accent-deep hover:underline">
                  Back to the dashboard
                </Link>
              }
            />
          </main>
        )}
      </div>
      <BottomNav onOpenMore={() => setDrawerOpen(true)} />
      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} />
    </div>
  );
}
