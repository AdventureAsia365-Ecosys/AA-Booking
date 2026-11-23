"use client";
// app/(staff)/suppliers/_parts/useSupplierActions.tsx — Deactivate / Activate a supplier (AA-822; UI spec §4 modal
// "Deactivate supplier", 08 conflict 4). Suppliers are never deleted. The API refuses a deactivation while a
// published contract is in force or upcoming (409 supplier_has_contracts); the dialog then says to expire it.
// Shared by the list row menu and the detail header. Needs supply.write (admin, contracting); activating is an
// approval and also needs supplier.activate (AA-1150), so a maker-only role only sees Deactivate.

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { suppliersApi, type Supplier } from "@/lib/api/suppliers";
import { ConfirmDialog, useCan, useToast } from "@/ui";
import { supplierError } from "./shared";
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { MSG } from "./useSupplierActions.messages";

// AA-1158: every new supplier starts pending; its creator never activates it, except the business owner, whose
// activation is flagged (`selfApproval`: the dialog says so). Deactivating a pending supplier rejects it.
type Target = Pick<Supplier, "code" | "company_name"> & { selfApproval?: boolean };
type Pending = { kind: "deactivate" | "activate"; supplier: Target } | null;

export function useSupplierActions() {
  const t = useT(MSG);
  const tx = useTx();
  const canWrite = useCan("supply.write");
  const canActivate = useCan("supplier.activate"); // held only next to supply.write (backend matrix test)
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
      error={tx(supplierError(run.error))}
      danger={pending?.kind === "deactivate"}
      title={pending?.kind === "deactivate" ? t("Deactivate supplier") : t("Activate supplier")}
      confirmLabel={pending?.kind === "deactivate" ? t("Deactivate") : t("Activate")}
      reason={pending?.kind === "deactivate" ? { label: t("Reason (optional)"), placeholder: t("e.g. closed for renovation") } : undefined}
      message={
        pending?.kind === "deactivate" ? (
          <>
            {t("Deactivate")} <strong className="text-ink">{name}</strong>{t("? It stays on record with its contracts and history, but no new contract can be added until it is activated again. A supplier with a published contract in force or starting later cannot be deactivated.")}
          </>
        ) : (
          <>
            {t("Activate")} <strong className="text-ink">{name}</strong>{t("? Contracting can add and publish contracts for it again.")}
            <SelfApprovalNote target={pending} />
          </>
        )
      }
    />
  );

  return {
    canWrite,
    canActivate,
    deactivate: (s: NonNullable<Pending>["supplier"]) => ask("deactivate", s),
    activate: (s: NonNullable<Pending>["supplier"]) => ask("activate", s),
    dialogs,
  };
}

/** AA-1158: the business owner activating a supplier she created (flagged and listed in the audit log). */
function SelfApprovalNote({ target }: { target: Pending }) {
  const t = useT(MSG);
  if (!target?.supplier.selfApproval) return null;
  return (
    <span className="mt-2 block">
      {t("You created this supplier. Activating it yourself is recorded as a business-owner self-approval.")}
    </span>
  );
}
