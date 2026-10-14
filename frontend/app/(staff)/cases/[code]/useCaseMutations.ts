"use client";
// app/(staff)/cases/[code]/useCaseMutations.ts
// AA-683 (fe-cases) — the write calls of the case workspace (spec §6.1): status change, assign, edit.
// Each success refreshes the case, its activity and the lists, and toasts with the case code.

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api/client";
import { TRIP_CASE_STATUS_META, useToast } from "@/ui";
import type { TripCaseStatus } from "@/lib/api/types";

export function useCaseRefresh(code: string) {
  const qc = useQueryClient();
  return () =>
    Promise.all([
      qc.invalidateQueries({ queryKey: ["trip-case", code] }),
      qc.invalidateQueries({ queryKey: ["trip-cases"] }),
    ]);
}

export function useChangeStatus(code: string) {
  const toast = useToast();
  const refresh = useCaseRefresh(code);
  return useMutation({
    mutationFn: ({ to, reason }: { to: TripCaseStatus; reason?: string }) => api.tripCases.changeStatus(code, to, reason || undefined),
    onSuccess: async (_, { to }) => {
      await refresh();
      toast.success(`Trip case ${code} is now ${TRIP_CASE_STATUS_META[to].label.toLowerCase()}`);
    },
  });
}

export function useAssign(code: string) {
  const toast = useToast();
  const refresh = useCaseRefresh(code);
  return useMutation({
    mutationFn: ({ cisUserId }: { cisUserId: string | null; quiet?: boolean }) => api.tripCases.assign(code, cisUserId, "sales"),
    onSuccess: async (res, { cisUserId, quiet }) => {
      await refresh();
      if (!quiet && res.changed) toast.success(cisUserId ? `Trip case ${code} assigned` : `Trip case ${code} is unassigned`);
    },
  });
}

export function useUpdateCase(code: string) {
  const toast = useToast();
  const refresh = useCaseRefresh(code);
  return useMutation({
    mutationFn: (body: Record<string, unknown>) => api.tripCases.update(code, body),
    onSuccess: async () => {
      await refresh();
      toast.success(`Trip case ${code} updated`);
    },
  });
}
