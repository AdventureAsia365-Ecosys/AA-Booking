"use client";
// app/(staff)/_parts/useRecordStatus.tsx — Deactivate / Activate a hotel or a service (AA-823, bk-010). Supply records
// are never deleted: an inactive one stays on record with its rates and history, but cannot be newly booked on a
// proposal item. A record of an inactive supplier cannot be activated (409 supplier_inactive). Needs supply.write.

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { supplyApi } from "@/lib/api/supply";
import { ConfirmDialog, useCan, useToast } from "@/ui";
import { supplyError } from "./supply";

type Kind = "hotel" | "service";
type Target = { kind: Kind; code: string; name: string; activate: boolean } | null;

export function useRecordStatus() {
  const canWrite = useCan("supply.write");
  const qc = useQueryClient();
  const toast = useToast();
  const [target, setTarget] = useState<Target>(null);

  const run = useMutation({
    mutationFn: ({ t, reason }: { t: NonNullable<Target>; reason: string }): Promise<unknown> =>
      t.kind === "hotel"
        ? supplyApi.setHotelStatus(t.code, t.activate, reason.trim())
        : supplyApi.setServiceStatus(t.code, t.activate, reason.trim()),
    onSuccess: (_res, { t }) => {
      qc.invalidateQueries({ queryKey: [t.kind === "hotel" ? "hotels" : "services"] });
      qc.invalidateQueries({ queryKey: [t.kind] });
      toast.success(`${t.name} ${t.activate ? "activated" : "deactivated"}.`);
      setTarget(null);
    },
  });

  function ask(kind: Kind, record: { code: string; name: string }, activate: boolean) {
    run.reset();
    setTarget({ kind, ...record, activate });
  }

  const noun = target?.kind === "hotel" ? "hotel" : "service";
  const dialogs = (
    <ConfirmDialog
      open={target !== null}
      onClose={() => setTarget(null)}
      onConfirm={(reason) => target && run.mutate({ t: target, reason })}
      busy={run.isPending}
      error={supplyError(run.error)}
      danger={!target?.activate}
      title={target?.activate ? `Activate ${noun}` : `Deactivate ${noun}`}
      confirmLabel={target?.activate ? "Activate" : "Deactivate"}
      reason={target?.activate ? undefined : { label: "Reason (optional)", placeholder: "e.g. closed for renovation" }}
      message={
        target?.activate ? (
          <>
            Activate <strong className="text-ink">{target?.name}</strong>? Sales can book it on proposal items again.
          </>
        ) : (
          <>
            Deactivate <strong className="text-ink">{target?.name}</strong>? It stays on record with its rates and history, and
            items that already book it keep it, but it cannot be added to a proposal until it is activated again.
          </>
        )
      }
    />
  );

  return { canWrite, ask, dialogs };
}
