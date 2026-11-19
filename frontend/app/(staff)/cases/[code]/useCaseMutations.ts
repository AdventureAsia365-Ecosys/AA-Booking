"use client";
// app/(staff)/cases/[code]/useCaseMutations.ts
// AA-683 (fe-cases) — the write calls of the case workspace (spec §6.1): status change, assign, edit.
// Each success refreshes the case, its activity and the lists, and toasts with the case code.

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api/client";
import { TRIP_CASE_STATUS_META, useToast } from "@/ui";
import type { TripCaseStatus } from "@/lib/api/types";
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { MSG } from "./useCaseMutations.messages";

export function useCaseRefresh(code: string) {
  const qc = useQueryClient();
  return () =>
    Promise.all([
      qc.invalidateQueries({ queryKey: ["trip-case", code] }),
      qc.invalidateQueries({ queryKey: ["trip-cases"] }),
    ]);
}

export function useChangeStatus(code: string) {
  const t = useT(MSG);
  const tx = useTx();
  const toast = useToast();
  const refresh = useCaseRefresh(code);
  return useMutation({
    mutationFn: ({ to, reason }: { to: TripCaseStatus; reason?: string }) => api.tripCases.changeStatus(code, to, reason || undefined),
    onSuccess: async (_, { to }) => {
      await refresh();
      toast.success(t("Trip case {0} is now {1}", code, tx(TRIP_CASE_STATUS_META[to].label).toLowerCase()));
    },
  });
}

export function useAssign(code: string) {
  const t = useT(MSG);
  const toast = useToast();
  const refresh = useCaseRefresh(code);
  return useMutation({
    mutationFn: ({ cisUserId }: { cisUserId: string | null; quiet?: boolean }) => api.tripCases.assign(code, cisUserId, "sales"),
    onSuccess: async (res, { cisUserId, quiet }) => {
      await refresh();
      if (!quiet && res.changed) toast.success(cisUserId ? t("Trip case {0} assigned", code) : t("Trip case {0} is unassigned", code));
    },
  });
}

export function useUpdateCase(code: string) {
  const t = useT(MSG);
  const toast = useToast();
  const refresh = useCaseRefresh(code);
  return useMutation({
    mutationFn: (body: Record<string, unknown>) => api.tripCases.update(code, body),
    onSuccess: async () => {
      await refresh();
      toast.success(t("Trip case {0} updated", code));
    },
  });
}
