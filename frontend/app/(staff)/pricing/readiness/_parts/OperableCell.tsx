"use client";
// app/(staff)/pricing/readiness/_parts/OperableCell.tsx — the "Can operate" cell of Price readiness (AA-1149).
// Ready / Warnings / Blocked / Exception until <date>, with the first reasons (a night without a hotel, a component
// without a published rate in the next 365 days, a supplier or item not active; warnings: a hotel move with no transfer,
// a guide on some days only). A blocked tour cannot be sold to agencies (tailor-made). An administrator opens the sale
// exception drawer from here. Kept apart from ReadinessView.tsx (complexity limit).

import { useState } from "react";
import type { OperableStatus, OperableSummary } from "@/lib/api/operability";
import { Button, DateText, Pill, useCan } from "@/ui";
import type { BookingTone } from "@/ui/booking/StatusBadge";
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { MSG } from "./OperableCell.messages";
import { findingText } from "./operableText";
import { SellExceptionDrawer } from "./SellExceptionDrawer";

const MAX_SHOWN = 3;

export const OPERABLE_META: Record<OperableStatus, { label: string; tone: BookingTone }> = {
  ready: { label: "Ready", tone: "success" },
  warnings: { label: "Warnings", tone: "warning" },
  blocked: { label: "Blocked", tone: "danger" },
  exception: { label: "Exception", tone: "purple" },
};

export function OperableCell({ tourKey, operable, onChanged }: { tourKey: string; operable: OperableSummary | null; onChanged: () => void }) {
  const t = useT(MSG);
  const tx = useTx();
  const canAllow = useCan("operable_exception.approve");
  const [open, setOpen] = useState(false);
  if (!operable) return <span className="text-muted">—</span>;
  const meta = OPERABLE_META[operable.status];
  const shown = operable.findings.slice(0, MAX_SHOWN);
  const more = operable.findings_total - shown.length;
  const canAct = canAllow && (operable.status === "blocked" || operable.status === "exception");
  return (
    <span className="flex min-w-0 flex-col items-start gap-1 text-[13px]" onClick={(e) => e.stopPropagation()}>
      <Pill tone={meta.tone} dot={false}>
        {tx(meta.label)}
        {operable.exception_until && (
          <>
            {" "}
            {t("until")} <DateText value={operable.exception_until} />
          </>
        )}
      </Pill>
      {shown.map((f) => (
        <span key={f.key} className={`break-words text-xs ${f.covered ? "text-muted line-through" : "text-body"}`}>
          {findingText(f, (k, v) => t(k, v))} ({f.rule})
        </span>
      ))}
      {more > 0 && <span className="text-xs text-muted">{t("and {0} more", more)}</span>}
      {canAct && (
        <Button size="sm" variant="ghost" onClick={() => setOpen(true)}>
          {operable.status === "blocked" ? t("Allow sale…") : t("Exception…")}
        </Button>
      )}
      {open && <SellExceptionDrawer tourKey={tourKey} onClose={() => setOpen(false)} onChanged={onChanged} />}
    </span>
  );
}
