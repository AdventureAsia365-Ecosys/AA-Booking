"use client";
// app/(staff)/catalog/_parts/useGeoStatus.tsx — AA-996: Deactivate / Activate a country or a destination in Booking
// (Figma DELETE COUNTRY, UNPUBLISHED COUNTRY, DELETE DESTINATION; docs/figma-coverage.md). Soft only: the place and its
// content stay in CIS, Booking records status = inactive with an optional reason (PATCH /booking/catalog/geo/…,
// bk-029, audited). Needs catalog.admin (admin). Same Figma dialog layout as every other soft delete (ConfirmDialog).

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { geoApi } from "@/lib/api/geo";
import { ConfirmDialog, useCan, useToast } from "@/ui";
import { useT } from "@/ui/core/useI18n";
import { MSG } from "./useGeoStatus.messages";

type Kind = "country" | "destination";
type Target = { kind: Kind; id: string; name: string; activate: boolean } | null;

export function useGeoStatus() {
  const tr = useT(MSG);
  const canWrite = useCan("catalog.admin");
  const qc = useQueryClient();
  const toast = useToast();
  const [target, setTarget] = useState<Target>(null);

  const run = useMutation({
    mutationFn: ({ t, reason }: { t: NonNullable<Target>; reason: string }): Promise<unknown> => {
      const body = t.activate ? { status: "active" as const } : { status: "inactive" as const, status_reason: reason || null };
      return t.kind === "country" ? geoApi.updateCountry(t.id, body) : geoApi.updateDestination(t.id, body);
    },
    onSuccess: (_res, { t }) => {
      qc.invalidateQueries({ queryKey: ["geo"] });
      toast.success(tr("{0} {1} in Booking.", t.name, t.activate ? "activated" : "deactivated"));
      setTarget(null);
    },
  });

  function ask(kind: Kind, record: { id: string; name: string }, activate: boolean) {
    run.reset();
    setTarget({ kind, ...record, activate });
  }

  const noun = target?.kind === "country" ? "country" : "destination";
  const dialogs = (
    <ConfirmDialog
      open={target !== null}
      onClose={() => setTarget(null)}
      onConfirm={(reason) => target && run.mutate({ t: target, reason })}
      busy={run.isPending}
      error={run.error ? (run.error as Error).message : null}
      danger={!target?.activate}
      title={target?.activate ? tr("Activate {0}", noun) : tr("Deactivate {0}", noun)}
      confirmLabel={target?.activate ? tr("Activate {0}", noun) : tr("Deactivate {0}", noun)}
      reason={target?.activate ? undefined : { label: tr("Reason (optional)"), placeholder: tr("e.g. no guides this season"), maxLength: 500 }}
      message={
        target?.activate ? (
          <>
            {tr("Activate")} <strong className="text-ink">{target?.name}</strong> {tr("in Booking? Staff see it as a place Booking sells again.")}
          </>
        ) : (
          <>
            {tr("Deactivate")} <strong className="text-ink">{target?.name}</strong> {tr("in Booking? Nothing is deleted: its content stays in CIS, trip cases and suppliers there are kept, and it shows as inactive until it is activated again.")}
          </>
        )
      }
    />
  );

  return { canWrite, ask, dialogs };
}
