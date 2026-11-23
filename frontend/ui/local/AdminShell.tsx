"use client";
// ui/local/AdminShell.tsx (AA-994 S4b: moved from ui/, the Booking shell; not part of the kit core)
// AA-683 — adapted copy of AA-CIS-App frontend/app/_kit/AdminShell.tsx (CIS commit 039bb250): Sidebar |
// (Topbar over the page), ⌘K palette, phone drawer + BottomNav. Booking addition: the page-level role
// check (lib/rbac.ts). A page the signed-in role may not open renders a "no access" state instead of
// its content, so it never fires API calls that would only answer 403.
// AA-1008 (S6e, root ADR 0016 decision 1): the frame is the kit core ShellFrame (layout, the phone drawer below 768px
// with its backdrop and Esc, the ⌘K palette state); this file is the Booking configuration of it. Every page renders
// its own PageFrame (`main="page"`), the "no access" state too.
// AA-1128 (B5, S283 UI audit): the UI language. The shell renders the ONE kit I18nProvider of the staff app (store
// ui/local/lang.ts, cookie `bk_lang`, English by default) around the frame and every page, and the Topbar carries the
// one EN | VI switch. The staff layout reads the cookie and passes `initialLang`, so a Vietnamese user gets Vietnamese
// HTML from the server (no English flash, no hydration mismatch).
// AA-1157: the owner's read-only "view as a lower role" banner sits in the banner slot (nothing outside a view).

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
import { ViewAsBanner } from "./ViewAsBanner";
import { canSeePage } from "@/lib/rbac";
import { useT } from "@/ui/core/useI18n";
import { MSG } from "./AdminShell.messages";
import { I18nProvider } from "../core/useI18n";
import type { Lang } from "../core/i18n";
import { langStore } from "./lang";
import "./phrases";

export function AdminShell({ children, initialLang }: { children: React.ReactNode; initialLang?: Lang }) {
  return (
    <I18nProvider store={langStore} initialLang={initialLang}>
      <ShellBody>{children}</ShellBody>
    </I18nProvider>
  );
}

function ShellBody({ children }: { children: React.ReactNode }) {
  const t = useT(MSG);
  const pathname = usePathname();
  // AA-1155 T7: the page rules follow the profile's capabilities (root spec R4.5), never the role name.
  const { permissions } = useStaff();
  const allowed = canSeePage(permissions, pathname);

  return (
    <ShellFrame
      drawerBelow={768}
      main="page"
      sidebarId="admin-sidebar-drawer"
      sidebar={(c) => <Sidebar drawerOpen={c.drawerOpen} onNavigate={c.closeDrawer} />}
      topbar={(c) => <Topbar onOpenSearch={c.openPalette} onOpenMenu={c.openDrawer} />}
      banner={() => <ViewAsBanner />}
      bottomNav={(c) => <BottomNav onOpenMore={c.openDrawer} />}
      palette={(c) => <CommandPalette open={c.paletteOpen} onClose={c.closePalette} />}
    >
      {allowed ? (
        children
      ) : (
        <PageFrame>
          <EmptyState
            icon={<ShieldOff size={30} />}
            title={t("You do not have access to this page")}
            description={t("Your Booking role does not include this page. Ask a Booking admin if you need it.")}
            action={
              <Link href="/" className="text-[13px] font-semibold text-accent-deep hover:underline">
                {t("Back to the dashboard")}
              </Link>
            }
          />
        </PageFrame>
      )}
    </ShellFrame>
  );
}
