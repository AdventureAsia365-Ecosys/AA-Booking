"use client";
// app/(staff)/cases/[code]/proposals/[n]/DetailsDrawer.tsx — the title and internal notes of one proposal version
// (moved out of page.tsx for AA-1128 B5, which kept page.tsx under its line budget).

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { ApiError } from "@/lib/api/client";
import { proposalsApi } from "@/lib/api/proposals";
import { FormDrawer, Input, Textarea, useToast } from "@/ui";
import { useT } from "@/ui/core/useI18n";
import { MSG } from "./DetailsDrawer.messages";

export function DetailsDrawer({ code, n, title, notes, onClose }: { code: string; n: number; title: string | null; notes: string | null; onClose: () => void }) {
  const tr = useT(MSG);
  const qc = useQueryClient();
  const toast = useToast();
  const [t, setT] = useState(title ?? "");
  const [nt, setNt] = useState(notes ?? "");
  const save = useMutation({
    mutationFn: () => {
      const body: { title?: string; notes?: string | null } = {};
      if (t.trim() && t.trim() !== (title ?? "")) body.title = t.trim();
      if (nt !== (notes ?? "")) body.notes = nt.trim() ? nt : null;
      return proposalsApi.update(code, n, body);
    },
    onSuccess: async (res) => {
      await Promise.all([
        qc.invalidateQueries({ queryKey: ["trip-case", code] }),
        qc.invalidateQueries({ queryKey: ["versions"] }),
      ]);
      toast.success(res.changed.length ? tr("V{0}: {1} updated", n, res.changed.join(", ")) : tr("Nothing changed"));
      onClose();
    },
  });
  const err = save.error as ApiError | null;
  const cls = "w-full rounded-md border border-line bg-card px-3 text-sm text-ink outline-none focus:border-accent-border";
  return (
    <FormDrawer
      open
      onClose={onClose}
      title={tr("Title and notes · V{0}", n)}
      onSubmit={() => save.mutate()}
      busy={save.isPending}
      dirty={t !== (title ?? "") || nt !== (notes ?? "")}
      error={err && !err.fieldErrors.length ? err.message : null}
      fieldErrors={err?.fieldErrors ?? []}
    >
      <label className="flex flex-col gap-1.5">
        <span className="text-[13px] font-medium text-ink">{tr("Proposal title")}</span>
        <Input value={t} maxLength={200} onChange={(e) => setT(e.target.value)} placeholder={tr("e.g. Vietnam north to south, family pace")} className={cls} />
        <span className="text-xs text-muted">{tr("Shown to the customer as the proposal name. A title cannot be cleared once set.")}</span>
      </label>
      <label className="flex flex-col gap-1.5">
        <span className="text-[13px] font-medium text-ink">{tr("Internal notes")}</span>
        <Textarea value={nt} rows={6} maxLength={10_000} onChange={(e) => setNt(e.target.value)} className={cls} />
      </label>
    </FormDrawer>
  );
}
