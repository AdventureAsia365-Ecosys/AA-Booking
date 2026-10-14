// app/(staff)/audit-log/page.tsx: Audit log (wave 2 route shipped early in S237, UI spec §3 / §6.3; Figma
// AUDIT LOGS (01)). The list itself is the client view below, over the live GET /booking/audit-logs.

import type { Metadata } from "next";
import { AuditLogView } from "./AuditLogView";

export const metadata: Metadata = { title: "Audit log" };

export default function AuditLogPage() {
  return <AuditLogView />;
}
