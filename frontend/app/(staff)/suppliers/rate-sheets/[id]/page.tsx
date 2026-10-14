// app/(staff)/suppliers/rate-sheets/[id]/page.tsx: Rate sheet review (wave 2, UI spec §3; not drawn (ADR 0005)).
// Registered now (Nghiệp S236 decision 3); renders its layout frame and "Coming in wave 2" until built.

import type { Metadata } from "next";
import { WaveFrame } from "@/ui/booking/WaveFrame";

export const metadata: Metadata = { title: "Rate sheet review" };

export default function SuppliersRateSheetsIdPage() {
  return (
    <WaveFrame
      wave={2}
      title="Rate sheet review"
      layout="detail"
      description="Check an uploaded rate sheet, then publish it. Publishing needs a second person (maker-checker)."
      purpose="Validation errors per sheet and row, a preview of the parsed rates, and Publish for someone other than the uploader (ADR 0005 §7–8)."
      tabs={["Validation", "Preview rows", "History"]}
      sections={["Rows", "Publish", "Upload"]}
      breadcrumbs={[{"label": "Suppliers", "href": "/suppliers"}, {"label": "Rate sheet"}]}
    />
  );
}
