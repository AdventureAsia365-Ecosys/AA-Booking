"use client";
// app/(staff)/suppliers/_parts/RateSheetDrawer.tsx — supplier rate-sheet Excel import (AA-1029, root ADR 0005 d7 / d8).
// Download the AA template, fill it (a Contract sheet and one sheet per service type, items by their HTL- / SVC- code),
// pick it: "Check" validates it with nothing written (dry run) and lists the rates or every problem by sheet, row and
// column; "Import as draft" creates one DRAFT contract with its rates. A second person publishes the draft from the
// Contracts table (maker-checker), and the prices of the trip plans that use these items follow by themselves.
// AA-1029: the Check result also lists the trip plans that use the sheet's items, whether each would have a rate for
// every component once the draft is published, and how many would move to priced (Pricing → Price readiness).
// AA-1153: optional Seasons and Hotel children sheets; hotels are matched by the supplier's own code for them too; the
// Check lists the business checks (errors stop the import) and the diff against the supplier's previous contract.

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Download, FileSpreadsheet } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import { suppliersApi, type RateSheetImpact, type RateSheetProblem, type RateSheetResult } from "@/lib/api/suppliers";
import { Button, FormDrawer, LinkButton, PriceReadinessBadge, useToast } from "@/ui";
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { useBookingFormat } from "@/ui/local/useBookingFormat";
import { MSG } from "./RateSheetDrawer.messages";
import { InventoryPreview } from "./InventoryTerms";
import { SheetReview } from "./RateReview";

const MAX_FILE_BYTES = 2 * 1024 * 1024;
const MAX_IMPACT_LISTED = 12;

function toBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).replace(/^data:[^,]*,/, ""));
    reader.onerror = () => reject(new Error("The file could not be read."));
    reader.readAsDataURL(file);
  });
}

function problemsOf(err: unknown): RateSheetProblem[] {
  if (!(err instanceof ApiError)) return [];
  const list = err.body.errors;
  return Array.isArray(list) ? (list as RateSheetProblem[]) : [];
}

/** The trip plans the sheet would price once the draft is published (read only; nothing is written by a check). */
function Impact({ impact }: { impact: RateSheetImpact }) {
  const t = useT(MSG);
  if (!impact.plans_total) {
    return <p className="text-xs text-muted">{t("No trip plan uses these hotels or services yet.")}</p>;
  }
  const moving = impact.would_move_to_priced;
  return (
    <div className="flex flex-col gap-1.5 rounded-md border border-line p-3 text-xs text-body" data-testid="rate-sheet-impact">
      <p className="font-medium text-ink">
        {t("{0} trip plan{1} these items. Once this draft is published, {2}", impact.plans_total, impact.plans_total > 1 ? "s use" : " uses", moving ? t("{0} would price from rates.", moving) : t("none would move to priced."))}
      </p>
      <ul className="flex flex-col gap-1.5">
        {impact.plans.slice(0, MAX_IMPACT_LISTED).map((p) => (
          <li key={p.tour_key} className="flex min-w-0 flex-col gap-0.5">
            <span className="flex min-w-0 flex-wrap items-center gap-1.5">
              <span className="min-w-0 break-words font-medium text-ink">{p.title ?? p.tour_key}</span>
              <PriceReadinessBadge status={p.status} />
              <span aria-hidden="true" className="text-muted">
                →
              </span>
              <span className="sr-only">{t("after publishing:")}</span>
              <PriceReadinessBadge status={p.status_after} />
            </span>
            <span className="text-muted">
              {t("{0} of {1} components from this sheet{2}{3}", p.components_from_sheet, p.components, p.all_rated_after
                ? t("; every component has a rate")
                : `; still without a rate: ${p.components_without_rate_after.map((c) => (c.room_type ? `${c.code} ${c.room_type}` : c.code)).join(", ")}`, p.missing_fx_after.length ? `; no exchange rate for ${p.missing_fx_after.join(", ")}` : "")}
            </span>
          </li>
        ))}
      </ul>
      {impact.plans.length > MAX_IMPACT_LISTED && <p className="text-muted">{t("… and {0} more", impact.plans.length - MAX_IMPACT_LISTED)}</p>}
      {impact.truncated && <p className="text-muted">{t("Only the first {0} plans were checked.", impact.plans.length)}</p>}
    </div>
  );
}

export function RateSheetDrawer({ supplierRef, onClose }: { supplierRef: string; onClose: () => void }) {
  const t = useT(MSG);
  const tx = useTx();
  const { formatNumber } = useBookingFormat();
  const qc = useQueryClient();
  const toast = useToast();
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<RateSheetResult | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);

  const send = useMutation({
    mutationFn: async (dryRun: boolean) => {
      if (!file) throw new Error(t("Pick the filled rate sheet first."));
      if (file.size > MAX_FILE_BYTES) throw new Error(t("A rate sheet is at most 2 MB."));
      return suppliersApi.importRateSheet(supplierRef, await toBase64(file), file.name, dryRun);
    },
    onSuccess: (out) => {
      if (out.dry_run) {
        setPreview(out);
        return;
      }
      qc.invalidateQueries({ queryKey: ["supplier", supplierRef] });
      toast.success(t("Draft contract created with {0} rates. A second person publishes it.", out.imported ?? 0));
      onClose();
    },
  });

  async function downloadTemplate() {
    try {
      const blob = await suppliersApi.rateSheetTemplate();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "aa-rate-sheet-template.xlsx";
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setLocalError(e instanceof Error ? e.message : t("The template could not be downloaded."));
    }
  }

  const problems = problemsOf(send.error);
  const error = localError ?? (send.error && !problems.length ? (send.error as Error).message : null);
  return (
    <FormDrawer
      open
      onClose={onClose}
      title={t("Import rate sheet")}
      submitLabel={t("Import as draft")}
      onSubmit={() => {
        setLocalError(null);
        send.mutate(false);
      }}
      busy={send.isPending}
      dirty={!!file}
      error={error ?? (problems.length ? t("{0} problem(s) in the sheet: nothing was imported.", problems.length) : null)}
      width={560}
    >
      <p className="text-[13px] text-muted">
        {t("One sheet per service type (Hotel, Transport, Guide, Entrance, Activity, Meal) and a Contract sheet. Items are matched by their hotel code and room type, or their service code. The import creates a draft contract; a second person publishes it.")}{" "}
        {t("Optional sheets: Seasons, Hotel children and Inventory (allotment, free sale, on request). To change a contract, download it from the Rates tab (Excel), edit it and import it here.")}
      </p>
      <LinkButton className="self-start" onClick={downloadTemplate}>
        <Download size={14} /> {t("Download the template")}
      </LinkButton>
      <label className="flex flex-col gap-1.5 text-[13px] font-medium text-ink">
        {t("Filled rate sheet (.xlsx)")}
        <input
          type="file"
          accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
          className="min-w-0 max-w-full text-xs font-normal"
          onChange={(e) => {
            setFile(e.target.files?.[0] ?? null);
            setPreview(null);
            send.reset();
          }}
        />
      </label>
      <Button className="self-start" variant="secondary" disabled={!file || send.isPending} onClick={() => send.mutate(true)}>
        <FileSpreadsheet size={14} /> {t("Check the sheet")}
      </Button>
      {problems.length > 0 && (
        <ul className="flex flex-col gap-1 rounded-md border border-danger-border p-3 text-xs text-danger" aria-label={t("Problems in the sheet")}>
          {problems.map((p, i) => (
            <li key={i}>
              {p.sheet}
              {p.row ? t(" row {0}", p.row) : ""}
              {p.column ? ` · ${p.column}` : ""}: {tx(p.message)}
            </li>
          ))}
        </ul>
      )}
      {preview && (
        <div className="flex flex-col gap-1 rounded-md border border-line p-3 text-xs text-body" data-testid="rate-sheet-preview">
          <p className="font-medium text-ink">
            {tx(preview.contract.title)} · {preview.contract.currency} {t("· {0} rates ready", preview.rates.length)}
          </p>
          <ul className="flex flex-col gap-0.5 text-muted">
            {preview.rates.slice(0, 12).map((r) => (
              <li key={`${r.sheet}-${r.row}`}>
                {t("{0} row {1}: {2} per {3}{4}{5}", r.sheet, r.row, formatNumber(r.net_amount), tx(r.unit.replace("_", " ")), r.season ? ` · ${r.season}` : "", r.pax_min || r.pax_max ? t(" · pax {0}–{1}", r.pax_min ?? 1, r.pax_max ?? t("more")) : "")}
              </li>
            ))}
            {preview.rates.length > 12 && <li>{t("… and {0} more", preview.rates.length - 12)}</li>}
          </ul>
          <InventoryPreview rows={preview.inventory} />
          {preview.ignored_sheets.length > 0 && <p className="text-muted">{t("Not read:")} {preview.ignored_sheets.join(", ")}</p>}
        </div>
      )}
      <SheetReview preview={preview} />
      {preview?.impact && <Impact impact={preview.impact} />}
    </FormDrawer>
  );
}
