"use client";
// app/(staff)/_parts/useRecordStatus.tsx — Deactivate / Activate a hotel or a service (AA-823, bk-010). Supply records
// are never deleted: an inactive one stays on record with its rates and history, but cannot be newly booked on a
// proposal item. A record of an inactive supplier cannot be activated (409 supplier_inactive). Needs supply.write;
// activating is an approval and also needs supplier.activate (AA-1155), so a maker-only role only sees Deactivate.

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { supplyApi } from "@/lib/api/supply";
import { ConfirmDialog, useCan, useToast } from "@/ui";
import { supplyError } from "./supply";
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { MSG } from "./useRecordStatus.messages";

type Kind = "hotel" | "service";
type Target = { kind: Kind; code: string; name: string; activate: boolean } | null;

/** Deactivate = supply.write; Activate = supply.write + supplier.activate (AA-1155: an approval, not a write). */
function toggleAllowed(active: boolean, canWrite: boolean, canActivate: boolean): boolean {
  return canWrite && (active || canActivate);
}

export function useRecordStatus() {
  const tr = useT(MSG);
  const tx = useTx();
  const canWrite = useCan("supply.write");
  const canActivate = useCan("supplier.activate");
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
      error={tx(supplyError(run.error))}
      danger={!target?.activate}
      title={target?.activate ? tr("Activate {0}", noun) : tr("Deactivate {0}", noun)}
      confirmLabel={target?.activate ? tr("Activate") : tr("Deactivate")}
      reason={target?.activate ? undefined : { label: tr("Reason (optional)"), placeholder: tr("e.g. closed for renovation") }}
      message={
        target?.activate ? (
          <>
            {tr("Activate")} <strong className="text-ink">{target?.name}</strong>{tr("? Sales can book it on proposal items again.")}
          </>
        ) : (
          <>
            {tr("Deactivate")} <strong className="text-ink">{target?.name}</strong>{tr("? It stays on record with its rates and history, and items that already book it keep it, but it cannot be added to a proposal until it is activated again.")}
          </>
        )
      }
    />
  );

  /** May this user flip a record that is `active` now? Deactivate = supply.write, Activate = supplier.activate. */
  const canToggle = (active: boolean) => toggleAllowed(active, canWrite, canActivate);

  return { canWrite, canToggle, ask, dialogs };
}
