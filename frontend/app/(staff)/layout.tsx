// app/(staff)/layout.tsx
// AA-683 — every staff page renders inside this layout. It asks the Booking API who the signed-in user
// is (GET /booking/staff/me with the Booking JWT from the cookie, refreshed by proxy.ts) and:
//   ok           → StaffProvider + AdminShell (role-aware nav and page gate);
//   401          → /login (session over);
//   403 inactive → "pending activation" screen (an admin suspended the profile, or a bridge-era row was
//                  deactivated when the CIS bridge was retired, AA-911);
//   403 no_booking_role → "no Booking role" screen with the CIS Platform → Staff link (AA-680: the role comes from
//                  the booking-* group of the company sign-in);
//   other 403    → "no Booking access" screen;
//   unavailable  → "Booking API unavailable" screen (network, 5xx, or the endpoint not deployed yet).

import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { ACCESS_COOKIE } from "@/lib/session";
import { CIS_STAFF_URL } from "@/lib/config";
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
        title="Your Booking access is not active"
        message="You are signed in, but your Booking profile is not active yet or was suspended. Ask a Booking admin to activate it on the Staff page."
      />
    );
  }
  if (me.kind === "forbidden" && me.code === "no_booking_role") {
    return (
      <SessionNotice
        tone="blocked"
        title="Your account has no Booking role"
        message="You signed in with your company e-mail, but no Booking role is set for you. Ask an owner to give you a Booking role in CIS → Platform → Staff, then sign in again."
        link={{ href: CIS_STAFF_URL, label: "Open CIS → Platform → Staff" }}
      />
    );
  }
  if (me.kind === "forbidden") {
    return (
      <SessionNotice
        tone="blocked"
        title="This account cannot use Booking"
        message={`Booking refused this account (${me.code || "forbidden"}). Ask a Booking admin to check your profile.`}
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
