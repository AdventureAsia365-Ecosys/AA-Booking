// app/(staff)/layout.tsx
// AA-683 — every staff page renders inside this layout. It asks the Booking API who the signed-in user
// is (GET /booking/staff/me with the Booking JWT from the cookie, refreshed by proxy.ts) and:
//   ok           → StaffProvider + AdminShell (role-aware nav and page gate);
//   401          → /login (session over);
//   403 inactive → "pending activation" screen (CIS non-admins start inactive, bk-003 rule);
//   other 403    → "no Booking access" screen;
//   unavailable  → "Booking API unavailable" screen (network, 5xx, or the endpoint not deployed yet).

import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { ACCESS_COOKIE } from "@/lib/session";
import { fetchStaffMe } from "@/lib/api/server";
import { AdminShell } from "@/ui/AdminShell";
import { StaffProvider } from "@/ui/StaffContext";
import { SessionNotice } from "./SessionNotice";

export const dynamic = "force-dynamic";

export default async function StaffLayout({ children }: { children: React.ReactNode }) {
  const token = (await cookies()).get(ACCESS_COOKIE)?.value;
  if (!token) redirect("/login?reason=expired");

  const me = await fetchStaffMe(token);
  if (me.kind === "unauthorized") redirect("/login?reason=expired");
  if (me.kind === "inactive") {
    return (
      <SessionNotice
        tone="pending"
        title="Your Booking access is pending activation"
        message="You are signed in with your CIS account. A Booking admin needs to activate your profile and give it a role before you can open Booking."
      />
    );
  }
  if (me.kind === "forbidden") {
    return (
      <SessionNotice
        tone="blocked"
        title="This account cannot use Booking"
        message={`Booking refused this CIS account (${me.code || "forbidden"}). Ask a Booking admin to check your role.`}
      />
    );
  }
  if (me.kind === "unavailable") {
    return (
      <SessionNotice
        tone="error"
        title="The Booking service is not answering"
        message={
          me.status === 404
            ? "The Booking API that this app points to does not have the staff endpoints yet (HTTP 404). They ship with the bk-003 / bk-005 backend."
            : `The Booking API did not answer${me.status ? ` (HTTP ${me.status})` : ""}. Try again in a moment.`
        }
        retry
      />
    );
  }

  return (
    <StaffProvider staff={me.staff}>
      <AdminShell>{children}</AdminShell>
    </StaffProvider>
  );
}
