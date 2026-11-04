"use client";
// app/(staff)/suppliers/_parts/RateSheetDrawer.tsx — supplier rate-sheet Excel import (AA-1029, root ADR 0005 d7 / d8).
// Download the AA template, fill it (a Contract sheet and one sheet per service type, items by their HTL- / SVC- code),
// pick it: "Check" validates it with nothing written (dry run) and lists the rates or every problem by sheet, row and
// column; "Import as draft" creates one DRAFT contract with its rates. A second person publishes the draft from the
// Contracts table (maker-checker), and the prices of the trip plans that use these items follow by themselves.

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Download, FileSpreadsheet } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import { suppliersApi, type RateSheetProblem, type RateSheetResult } from "@/lib/api/suppliers";
import { Button, FormDrawer, LinkButton, useToast } from "@/ui";

const MAX_FILE_BYTES = 2 * 1024 * 1024;

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

export function RateSheetDrawer({ supplierRef, onClose }: { supplierRef: string; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<RateSheetResult | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);

  const send = useMutation({
    mutationFn: async (dryRun: boolean) => {
      if (!file) throw new Error("Pick the filled rate sheet first.");
      if (file.size > MAX_FILE_BYTES) throw new Error("A rate sheet is at most 2 MB.");
      return suppliersApi.importRateSheet(supplierRef, await toBase64(file), file.name, dryRun);
    },
    onSuccess: (out) => {
      if (out.dry_run) {
        setPreview(out);
        return;
      }
      qc.invalidateQueries({ queryKey: ["supplier", supplierRef] });
      toast.success(`Draft contract created with ${out.imported ?? 0} rates. A second person publishes it.`);
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
      setLocalError(e instanceof Error ? e.message : "The template could not be downloaded.");
    }
  }

  const problems = problemsOf(send.error);
  const error = localError ?? (send.error && !problems.length ? (send.error as Error).message : null);
  return (
    <FormDrawer
      open
      onClose={onClose}
      title="Import rate sheet"
      submitLabel="Import as draft"
      onSubmit={() => {
        setLocalError(null);
        send.mutate(false);
      }}
      busy={send.isPending}
      dirty={!!file}
      error={error ?? (problems.length ? `${problems.length} problem(s) in the sheet: nothing was imported.` : null)}
      width={560}
    >
      <p className="text-[13px] text-muted">
        One sheet per service type (Hotel, Transport, Guide, Entrance, Activity, Meal) and a Contract sheet. Items are matched by their hotel
        code and room type, or their service code. The import creates a draft contract; a second person publishes it.
      </p>
      <LinkButton className="self-start" onClick={downloadTemplate}>
        <Download size={14} /> Download the template
      </LinkButton>
      <label className="flex flex-col gap-1.5 text-[13px] font-medium text-ink">
        Filled rate sheet (.xlsx)
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
        <FileSpreadsheet size={14} /> Check the sheet
      </Button>
      {problems.length > 0 && (
        <ul className="flex flex-col gap-1 rounded-md border border-danger-border p-3 text-xs text-danger" aria-label="Problems in the sheet">
          {problems.map((p, i) => (
            <li key={i}>
              {p.sheet}
              {p.row ? ` row ${p.row}` : ""}
              {p.column ? ` · ${p.column}` : ""}: {p.message}
            </li>
          ))}
        </ul>
      )}
      {preview && (
        <div className="flex flex-col gap-1 rounded-md border border-line p-3 text-xs text-body" data-testid="rate-sheet-preview">
          <p className="font-medium text-ink">
            {preview.contract.title} · {preview.contract.currency} · {preview.rates.length} rates ready
          </p>
          <ul className="flex flex-col gap-0.5 text-muted">
            {preview.rates.slice(0, 12).map((r) => (
              <li key={`${r.sheet}-${r.row}`}>
                {r.sheet} row {r.row}: {r.net_amount} per {r.unit.replace("_", " ")}
                {r.season ? ` · ${r.season}` : ""}
                {r.pax_min || r.pax_max ? ` · pax ${r.pax_min ?? 1}–${r.pax_max ?? "more"}` : ""}
              </li>
            ))}
            {preview.rates.length > 12 && <li>… and {preview.rates.length - 12} more</li>}
          </ul>
          {preview.ignored_sheets.length > 0 && <p className="text-muted">Not read: {preview.ignored_sheets.join(", ")}</p>}
        </div>
      )}
    </FormDrawer>
  );
}
