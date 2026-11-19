// app/(staff)/layout.tsx
// AA-683 — every staff page renders inside this layout. It asks the Booking API who the signed-in user
// is (GET /booking/staff/me with the Booking JWT from the cookie, refreshed by proxy.ts) and:
//   ok           → StaffProvider + AdminShell (role-aware nav and page gate);
//   401          → /login (session over);
//   403 inactive → "pending activation" screen (legacy CIS non-admins start inactive, bk-003 rule; an admin can
//                  also suspend a profile);
//   403 no_booking_role → "no Booking role" screen with the CIS Platform → Staff link (AA-680: the role comes from
//                  the booking-* group of the company sign-in);
//   other 403    → "no Booking access" screen;
//   unavailable  → "Booking API unavailable" screen (network, 5xx, or the endpoint not deployed yet).
// AA-1128 (B5): the UI language comes from the `bk_lang` cookie (kit core/i18n.ts, English when absent): the notices
// are translated here on the server, and the shell's provider starts in that language (no English flash).

import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { ACCESS_COOKIE } from "@/lib/session";
import { B2C_INTAKE_OPEN, CIS_STAFF_URL } from "@/lib/config";
import { fetchStaffMe } from "@/lib/api/server";
import { SessionNotice } from "./SessionNotice";
// Server component: the shell modules directly, not the "@/ui" barrel (which re-exports client and server modules).
import { AdminShell } from "@/ui/local/AdminShell";
import { StaffProvider } from "@/ui/local/StaffContext";
import { withoutB2cWrites } from "@/ui/booking/b2cFrozen";
import { langFromCookie, translator, type Lang } from "@/ui/core/i18n";
import { langStore } from "@/ui/local/lang";
import { MSG } from "./layout.messages";

export const dynamic = "force-dynamic";

type MeAnswer = Awaited<ReturnType<typeof fetchStaffMe>>;

/** The full-page notice for an answer that does not open the app (null when the staff member gets the shell). */
function staffNotice(me: MeAnswer, lang: Lang): React.ReactNode {
  const t = translator(MSG, lang);
  if (me.kind === "inactive") {
    return <SessionNotice lang={lang} tone="pending" title={t("inactiveTitle")} message={t("inactiveMessage")} />;
  }
  if (me.kind === "forbidden" && me.code === "no_booking_role") {
    return (
      <SessionNotice
        lang={lang}
        tone="blocked"
        title={t("noRoleTitle")}
        message={t("noRoleMessage")}
        link={{ href: CIS_STAFF_URL, label: t("noRoleLink") }}
      />
    );
  }
  if (me.kind === "forbidden") {
    return (
      <SessionNotice lang={lang} tone="blocked" title={t("forbiddenTitle")} message={t("forbiddenMessage", me.code || "forbidden")} />
    );
  }
  if (me.kind === "unavailable") {
    return (
      <SessionNotice
        lang={lang}
        tone="error"
        title={t("downTitle")}
        message={me.status === 404 ? t("downNoEndpoints") : t("downMessage", me.status ? ` (HTTP ${me.status})` : "")}
        retry
      />
    );
  }
  return null;
}

export default async function StaffLayout({ children }: { children: React.ReactNode }) {
  const jar = await cookies();
  const token = jar.get(ACCESS_COOKIE)?.value;
  if (!token) redirect("/login?reason=expired");
  const lang = langFromCookie(jar.get(langStore.cookieName)?.value);

  const me = await fetchStaffMe(token);
  if (me.kind === "unauthorized") redirect("/login?reason=expired");
  const notice = staffNotice(me, lang);
  if (notice || me.kind !== "ok") return notice;

  // AA-1037 (root ADR 0019 d8): while the B2C part is closed no page offers a write on customers, customer invoices or
  // payments (the API answers 410 b2b_only); the pages read these permissions with useCan.
  const staff = { ...me.staff, permissions: withoutB2cWrites(me.staff.permissions, B2C_INTAKE_OPEN) };
  return (
    <StaffProvider staff={staff}>
      <AdminShell initialLang={lang}>{children}</AdminShell>
    </StaffProvider>
  );
}
