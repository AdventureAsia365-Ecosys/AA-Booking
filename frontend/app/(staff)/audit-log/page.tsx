// app/(staff)/audit-log/page.tsx: Audit log (wave 2, UI spec §3; Figma AUDIT LOGS (01)).
// Registered now (Nghiệp S236 decision 3); renders its layout frame and "Coming in wave 2" until built.

import type { Metadata } from "next";
import { WaveFrame } from "@/ui/booking/WaveFrame";

export const metadata: Metadata = { title: "Audit log" };

export default function AuditLogPage() {
  return (
    <WaveFrame
      wave={2}
      title="Audit log"
      layout="list"
      description="Every significant staff action in Booking, for accountability and troubleshooting."
      purpose="A searchable record of every staff write (who, what, when, before / after), with a CSV export."
      actions={["Export CSV"]}
      filters={["Search description or action", "Date range", "Module", "Staff member"]}
      columns={["Date & time", "Staff", "Action", "Module", "Description"]}
    />
  );
}
