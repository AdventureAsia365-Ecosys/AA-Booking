"use client";
// app/(staff)/suppliers/_parts/useSupplierActions.tsx — Deactivate / Activate a supplier (AA-822; UI spec §4 modal
// "Deactivate supplier", 08 conflict 4). Suppliers are never deleted. The API refuses a deactivation while a
// published contract is in force or upcoming (409 supplier_has_contracts); the dialog then says to expire it.
// Shared by the list row menu and the detail header. Needs supply.write (admin, contracting).

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { suppliersApi, type Supplier } from "@/lib/api/suppliers";
import { ConfirmDialog, useCan, useToast } from "@/ui";
import { supplierError } from "./shared";

type Pending = { kind: "deactivate" | "activate"; supplier: Pick<Supplier, "code" | "company_name"> } | null;

export function useSupplierActions() {
  const canWrite = useCan("supply.write");
  const qc = useQueryClient();
  const toast = useToast();
  const [pending, setPending] = useState<Pending>(null);

  const run = useMutation({
    mutationFn: ({ p, reason }: { p: NonNullable<Pending>; reason: string }) =>
      p.kind === "deactivate" ? suppliersApi.deactivate(p.supplier.code, reason.trim()) : suppliersApi.activate(p.supplier.code),
    onSuccess: (_res, { p }) => {
      qc.invalidateQueries({ queryKey: ["suppliers"] });
      qc.invalidateQueries({ queryKey: ["supplier"] });
      toast.success(`${p.supplier.company_name} ${p.kind === "deactivate" ? "deactivated" : "activated"}.`);
      setPending(null);
    },
  });

  function ask(kind: NonNullable<Pending>["kind"], supplier: NonNullable<Pending>["supplier"]) {
    run.reset();
    setPending({ kind, supplier });
  }

  const name = pending?.supplier.company_name ?? "";
  const dialogs = (
    <ConfirmDialog
      open={pending !== null}
      onClose={() => setPending(null)}
      onConfirm={(reason) => pending && run.mutate({ p: pending, reason })}
      busy={run.isPending}
      error={supplierError(run.error)}
      danger={pending?.kind === "deactivate"}
      title={pending?.kind === "deactivate" ? "Deactivate supplier" : "Activate supplier"}
      confirmLabel={pending?.kind === "deactivate" ? "Deactivate" : "Activate"}
      reason={pending?.kind === "deactivate" ? { label: "Reason (optional)", placeholder: "e.g. closed for renovation" } : undefined}
      message={
        pending?.kind === "deactivate" ? (
          <>
            Deactivate <strong className="text-ink">{name}</strong>? It stays on record with its contracts and history, but no
            new contract can be added until it is activated again. A supplier with a published contract in force or starting
            later cannot be deactivated.
          </>
        ) : (
          <>
            Activate <strong className="text-ink">{name}</strong>? Contracting can add and publish contracts for it again.
          </>
        )
      }
    />
  );

  return {
    canWrite,
    deactivate: (s: NonNullable<Pending>["supplier"]) => ask("deactivate", s),
    activate: (s: NonNullable<Pending>["supplier"]) => ask("activate", s),
    dialogs,
  };
}
