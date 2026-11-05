"use client";
// ui/local/AdminShell.tsx (AA-994 S4b: moved from ui/, the Booking shell; not part of the kit core)
// AA-683 — adapted copy of AA-CIS-App frontend/app/_kit/AdminShell.tsx (CIS commit 039bb250): Sidebar |
// (Topbar over the page), ⌘K palette, phone drawer + BottomNav. Booking addition: the page-level role
// check (lib/rbac.ts). A page the signed-in role may not open renders a "no access" state instead of
// its content, so it never fires API calls that would only answer 403.
// AA-1008 (S6e, root ADR 0016 decision 1): the frame is the kit core ShellFrame (layout, the phone drawer below 768px
// with its backdrop and Esc, the ⌘K palette state); this file is the Booking configuration of it. Every page renders
// its own PageFrame (`main="page"`), the "no access" state too.

import { usePathname } from "next/navigation";
import Link from "next/link";
import { ShieldOff } from "lucide-react";
import { Sidebar } from "./Sidebar";
import { Topbar } from "./Topbar";
import { BottomNav } from "./BottomNav";
import { CommandPalette } from "./CommandPalette";
import { EmptyState } from "../core/primitives";
import { PageFrame } from "../booking/PageFrame";
import { ShellFrame } from "../core/ShellFrame";
import { useStaff } from "./StaffContext";
import { canSeePage } from "@/lib/rbac";

export function AdminShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const { role } = useStaff();
  const allowed = canSeePage(role, pathname);

  return (
    <ShellFrame
      drawerBelow={768}
      main="page"
      sidebarId="admin-sidebar-drawer"
      sidebar={(c) => <Sidebar drawerOpen={c.drawerOpen} onNavigate={c.closeDrawer} />}
      topbar={(c) => <Topbar onOpenSearch={c.openPalette} onOpenMenu={c.openDrawer} />}
      bottomNav={(c) => <BottomNav onOpenMore={c.openDrawer} />}
      palette={(c) => <CommandPalette open={c.paletteOpen} onClose={c.closePalette} />}
    >
      {allowed ? (
        children
      ) : (
        <PageFrame>
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
        </PageFrame>
      )}
    </ShellFrame>
  );
}
