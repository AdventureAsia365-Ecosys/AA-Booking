// app/(staff)/trip-checks/page.tsx: Trip checks (wave 3, UI spec §3; Figma TRIP CHECKS (03)).
// Registered now (Nghiệp S236 decision 3); renders its layout frame and "Coming in wave 3" until built.

import type { Metadata } from "next";
import { WaveFrame } from "@/ui/booking/WaveFrame";

export const metadata: Metadata = { title: "Trip checks" };

export default function TripChecksPage() {
  return (
    <WaveFrame
      wave={3}
      title="Trip checks"
      layout="list"
      description="Pre-trip tasks across all trip cases: documents, vouchers, confirmations."
      purpose="Every pre-departure task with its due date and owner, across cases."
      filters={["Search case or task", "Status", "Assignee", "Due"]}
      columns={["Task", "Trip case", "Customer", "Due", "Assignee", "Status"]}
    />
  );
}
