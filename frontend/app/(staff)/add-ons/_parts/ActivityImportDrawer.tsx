"use client";
// app/(staff)/add-ons/_parts/ActivityImportDrawer.tsx — import a supplier's activity catalogue (AA-1151, bk-068).
// Pick the AA supplier, download the template, fill it (a Contract sheet + one Activities row per activity: places,
// sale rules, net prices), "Check" validates with nothing written; "Import as drafts" creates per row an activity
// service, a draft add-on and its rates in one draft contract. Second persons publish the contract and the add-ons.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, FileSpreadsheet } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import { addonsApi, type ActivitySheetProblem, type ActivitySheetResult } from "@/lib/api/addons";
import { suppliersApi } from "@/lib/api/suppliers";
import { Button, Field, FormDrawer, LinkButton, Select, useToast } from "@/ui";
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { MSG } from "./AddonsView.messages";

const MAX_FILE_BYTES = 2 * 1024 * 1024;
const MAX_LISTED = 12;

function toBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).replace(/^data:[^,]*,/, ""));
    reader.onerror = () => reject(new Error("The file could not be read."));
    reader.readAsDataURL(file);
  });
}

function problemsOf(err: unknown): ActivitySheetProblem[] {
  if (!(err instanceof ApiError)) return [];
  const list = err.body.errors;
  return Array.isArray(list) ? (list as ActivitySheetProblem[]) : [];
}

export function ActivityImportDrawer({ onClose }: { onClose: () => void }) {
  const t = useT(MSG);
  const tx = useTx();
  const qc = useQueryClient();
  const toast = useToast();
  const [supplier, setSupplier] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<ActivitySheetResult | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);
  const suppliers = useQuery({
    queryKey: ["suppliers", "activity"],
    queryFn: () => suppliersApi.list({ category: "activity", status: "active", limit: 200 }),
  });

  const send = useMutation({
    mutationFn: async (dryRun: boolean) => {
      if (!supplier) throw new Error(t("Pick the supplier first."));
      if (!file) throw new Error(t("Pick the filled activity sheet first."));
      if (file.size > MAX_FILE_BYTES) throw new Error(t("An activity sheet is at most 2 MB."));
      return addonsApi.importSheet(supplier, await toBase64(file), file.name, dryRun);
    },
    onSuccess: (out) => {
      if (out.dry_run) {
        setPreview(out);
        return;
      }
      qc.invalidateQueries({ queryKey: ["add-ons"] });
      toast.success(t("{0} activities imported as drafts. Second persons publish the contract and the add-ons.", out.imported ?? 0));
      onClose();
    },
  });

  async function downloadTemplate() {
    try {
      const blob = await addonsApi.template();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "aa-activity-catalogue-template.xlsx";
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
      title={t("Import activities")}
      submitLabel={t("Import as drafts")}
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
        {t("A Contract sheet and one Activities row per activity: places, sale rules and net prices. Each row becomes an activity, a draft add-on and its rates in one draft contract.")}
      </p>
      <Field label={t("Supplier")}>
        <Select
          value={supplier}
          onChange={(e) => {
            setSupplier(e.target.value);
            setPreview(null);
            send.reset();
          }}
          options={[
            { value: "", label: t("Pick an AA supplier") },
            ...(suppliers.data?.items ?? []).map((s) => ({ value: s.code, label: `${s.company_name} (${s.code})` })),
          ]}
        />
      </Field>
      <LinkButton className="self-start" onClick={downloadTemplate}>
        <Download size={14} /> {t("Download the template")}
      </LinkButton>
      <label className="flex flex-col gap-1.5 text-[13px] font-medium text-ink">
        {t("Filled activity sheet (.xlsx)")}
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
      <Button className="self-start" variant="secondary" disabled={!file || !supplier || send.isPending} onClick={() => send.mutate(true)}>
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
        <div className="flex flex-col gap-1 rounded-md border border-line p-3 text-xs text-body" data-testid="activity-sheet-preview">
          <p className="font-medium text-ink">{t("{0} activities ready", preview.activities.length)}</p>
          <ul className="flex flex-col gap-0.5 text-muted">
            {preview.activities.slice(0, MAX_LISTED).map((a) => (
              <li key={a.row}>
                {t("Row {0}: {1} at {2}", a.row, a.title, a.places.map((p) => p.name).join(", "))}
              </li>
            ))}
            {preview.activities.length > MAX_LISTED && <li>{t("… and {0} more", preview.activities.length - MAX_LISTED)}</li>}
          </ul>
        </div>
      )}
    </FormDrawer>
  );
}
