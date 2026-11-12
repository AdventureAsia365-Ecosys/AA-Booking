"use client";
// app/(staff)/pricing/workbench/_parts/ImportDrawer.tsx — AA-1091 item 3: the price setup workbook for many tours.
// Download it blank or prefilled with the tours of the current filter (their hotels and services, hand prices and
// departures, so an unchanged file changes nothing), fill it, pick it: "Check" runs the import as a DRY RUN (nothing
// written) and lists every problem by sheet, row and column, or per tour what would change and its price status
// after; "Import" writes it through the same services as the single forms (POST /booking/catalog/price-import).

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Download, FileSpreadsheet } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import {
  priceWorkbenchApi,
  type ImportProblem,
  type ImportResult,
  type ImportTour,
  type WorkbenchFilters,
} from "@/lib/api/priceWorkbench";
import { problemLine, problemsBySheet } from "@/ui/booking/priceWorkbench";
import { Button, Drawer, LightTable, LinkButton, Pill, useT, useToast, type LightColumn } from "@/ui";
import { M, type MessageKey } from "./messages";

const MAX_FILE_BYTES = 2 * 1024 * 1024;
const MAX_PROBLEMS_SHOWN = 60;

function toBase64(file: File, failed: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).replace(/^data:[^,]*,/, ""));
    reader.onerror = () => reject(new Error(failed));
    reader.readAsDataURL(file);
  });
}

function problemsOf(err: unknown): ImportProblem[] {
  if (!(err instanceof ApiError)) return [];
  const list = err.body.errors;
  return Array.isArray(list) ? (list as ImportProblem[]) : [];
}

function save(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

export function ImportDrawer({ filters, onClose }: { filters: WorkbenchFilters; onClose: () => void }) {
  const t = useT(M);
  const toast = useToast();
  const qc = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<ImportResult | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);

  const send = useMutation({
    mutationFn: async (dryRun: boolean) => {
      if (!file) throw new Error(t("importPickFirst"));
      if (file.size > MAX_FILE_BYTES) throw new Error(t("importTooLarge"));
      return priceWorkbenchApi.importWorkbook(await toBase64(file, t("importReadFailed")), file.name, dryRun);
    },
    onSuccess: (out) => {
      if (out.dry_run) {
        setPreview(out);
        return;
      }
      for (const key of [["price-workbench"], ["price-readiness"], ["trip-plans"]]) qc.invalidateQueries({ queryKey: key });
      toast.success(t("importDone", out.totals.tours));
      onClose();
    },
  });

  async function download(blank: boolean) {
    setLocalError(null);
    try {
      const { blob, filename } = await priceWorkbenchApi.template(filters, blank);
      save(blob, filename);
    } catch (e) {
      setLocalError(e instanceof Error ? e.message : String(e));
    }
  }

  const statusLabel = (s: string | null) => (s ? (t(`status_${s}` as MessageKey) as string) : "—");
  const columns: LightColumn<ImportTour>[] = [
    {
      id: "tour",
      header: t("colTour"),
      width: "minmax(0,2fr)",
      cell: (r) => <span className="min-w-0 break-words font-medium text-ink">{r.title ?? r.tour_key}</span>,
    },
    {
      id: "changes",
      header: t("importChanged"),
      width: "minmax(0,2fr)",
      cell: (r) => {
        const parts = [
          r.components_changed ? `${t("check_components")}: ${r.components_before} → ${r.components_after}` : null,
          r.hand_rows_after !== r.hand_rows_before || r.computed_rows_replaced
            ? `${t("check_price")}: ${r.hand_rows_before} → ${r.hand_rows_after}`
            : null,
          r.departures_new.length ? `${t("check_departures")}: +${r.departures_new.length}` : null,
          r.sellable_after !== r.sellable_before ? `${t("check_sellable")}: ${r.sellable_after ? "✓" : "✗"}` : null,
        ].filter(Boolean);
        return <span className="text-xs text-body">{parts.length ? parts.join(" · ") : t("importNoChange")}</span>;
      },
    },
    {
      id: "status",
      header: t("importStatus"),
      width: "minmax(0,1.6fr)",
      cell: (r) => (
        <span className="flex flex-wrap items-center gap-1 text-xs">
          <span className="text-muted">{statusLabel(r.status_before)}</span>
          <span aria-hidden="true">→</span>
          <Pill tone={r.status_after === "priced" || r.status_after === "hand_priced" ? "success" : "neutral"}>
            {statusLabel(r.status_after)}
          </Pill>
        </span>
      ),
    },
  ];

  const problems = problemsOf(send.error);
  const error = localError ?? (send.error && !problems.length ? (send.error as Error).message : null);
  return (
    <Drawer
      open
      onClose={() => !send.isPending && onClose()}
      title={t("importTitle")}
      width={720}
      footer={
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose} disabled={send.isPending}>
            {t("cancel")}
          </Button>
          <Button variant="secondary" disabled={!file || send.isPending} onClick={() => send.mutate(true)}>
            {t("importCheck")}
          </Button>
          <Button variant="primary" disabled={!file || !preview || send.isPending} onClick={() => send.mutate(false)}>
            {send.isPending ? t("saving") : t("importSubmit")}
          </Button>
        </div>
      }
    >
      <div className="flex min-w-0 flex-col gap-4" data-testid="price-import-drawer">
        <p className="text-[13px] text-muted">{t("importIntro")}</p>
        <div className="flex flex-wrap gap-3">
          <LinkButton onClick={() => download(false)}>
            <Download size={14} /> {t("importPrefilled")}
          </LinkButton>
          <LinkButton onClick={() => download(true)}>
            <FileSpreadsheet size={14} /> {t("importBlank")}
          </LinkButton>
        </div>
        <label className="flex flex-col gap-1.5 text-[13px] font-medium text-ink">
          {t("importFile")}
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
        {error && (
          <p className="rounded-md border border-danger-border bg-danger-soft px-3 py-2 text-[13px] text-body" role="alert">
            {error}
          </p>
        )}
        {!!problems.length && (
          <div className="flex flex-col gap-2 rounded-md border border-danger-border p-3" role="alert" data-testid="price-import-problems">
            <p className="text-[13px] font-medium text-ink">{t("importProblems", problems.length)}</p>
            {problemsBySheet(problems.slice(0, MAX_PROBLEMS_SHOWN)).map(([sheet, list]) => (
              <ul key={sheet} className="flex flex-col gap-0.5 text-xs text-body">
                {list.map((p) => (
                  <li key={`${p.sheet}-${p.row}-${p.column}-${p.code}`} className="break-words">
                    {problemLine(p)}
                  </li>
                ))}
              </ul>
            ))}
          </div>
        )}
        {preview && (
          <div className="flex min-w-0 flex-col gap-2" data-testid="price-import-preview">
            <p className="text-[13px] font-medium text-ink">{t("importChecked", preview.totals.tours)}</p>
            <p className="text-xs text-muted">
              {t(
                "importTotals",
                preview.totals.components_changed,
                preview.totals.price_tours,
                preview.totals.departures_new,
                preview.totals.sellable_changes,
              )}
            </p>
            {!!preview.totals.departures_existing && (
              <p className="text-xs text-muted">{t("importExisting", preview.totals.departures_existing)}</p>
            )}
            <LightTable label={t("importTitle")} rows={preview.tours} columns={columns} rowKey={(r) => r.tour_key} />
          </div>
        )}
      </div>
    </Drawer>
  );
}
