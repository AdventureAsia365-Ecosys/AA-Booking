// app/(staff)/suppliers/rate-sheets/[id]/page.tsx: Rate sheet review (wave 2, UI spec §3; not drawn (ADR 0005)).
// Registered now (Nghiệp S236 decision 3); renders its layout frame and "Coming in wave 2" until built.

import type { Metadata } from "next";
import { WaveFrame } from "@/ui/booking/WaveFrame";
import { useT } from "@/ui/core/useI18n";
import { MSG } from "./page.messages";

export const metadata: Metadata = { title: "Rate sheet review" };

export default function SuppliersRateSheetsIdPage() {
  const t = useT(MSG);
  return (
    <WaveFrame
      wave={2}
      title={t("Rate sheet review")}
      layout="detail"
      description={t("Check an uploaded rate sheet, then publish it. Publishing needs a second person (maker-checker).")}
      purpose={t("Validation errors per sheet and row, a preview of the parsed rates, and Publish for someone other than the uploader (ADR 0005 §7–8).")}
      tabs={[t("Validation"), t("Preview rows"), t("History")]}
      sections={[t("Rows"), t("Publish"), t("Upload")]}
      breadcrumbs={[{"label": t("Suppliers"), "href": "/suppliers"}, {"label": t("Rate sheet")}]}
    />
  );
}
