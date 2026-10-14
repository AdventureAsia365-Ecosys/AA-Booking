"use client";
// app/(staff)/customers/_parts/useCustomerActions.tsx
// AA-683 (fe-people) — Block / Unblock and Archive for a customer, shared by the list row menu and the
// detail header (UI spec §4.12; Figma USER MANAGEMENT-4 "Block Customer", DELETE USER as a soft delete).
//   Block / Unblock: PATCH /booking/customers/{ref} {is_blocked}. Admin only (`customer.admin`).
//   Archive: DELETE /booking/customers/{ref} (sets deleted_at; open new inquiries are cancelled with the
//     reason "customer archived"). Refused with 409 `customer_has_cases` once any case went past
//     new_inquiry; the dialog then says to block instead. No hard delete anywhere (Q23).

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "@/lib/api/client";
import type { CustomerSummary } from "@/lib/api/types";
import { ConfirmDialog, useCan, useToast } from "@/ui";

type Pending = { kind: "block" | "unblock" | "archive"; customer: CustomerSummary } | null;

function refOf(c: CustomerSummary) {
  return c.code ?? c.id;
}

/** Copy for an archive refusal; the API body names the blocking cases but ApiError keeps only code + message. */
export function archiveErrorText(err: unknown): string {
  if (err instanceof ApiError && err.code === "customer_has_cases") {
    return "This customer has a trip case past New inquiry, so it cannot be archived. Block the customer instead.";
  }
  return err instanceof Error ? err.message : "Could not archive the customer.";
}

export function useCustomerActions({ onArchived }: { onArchived?: () => void } = {}) {
  const canAdmin = useCan("customer.admin");
  const qc = useQueryClient();
  const toast = useToast();
  const [pending, setPending] = useState<Pending>(null);

  const run = useMutation({
    mutationFn: async (p: NonNullable<Pending>) => {
      if (p.kind === "archive") return api.customers.archive(refOf(p.customer));
      return api.customers.update(refOf(p.customer), { is_blocked: p.kind === "block" });
    },
    onSuccess: (_res, p) => {
      qc.invalidateQueries({ queryKey: ["customers"] });
      qc.invalidateQueries({ queryKey: ["customer"] });
      const name = p.customer.name ?? refOf(p.customer);
      toast.success(p.kind === "archive" ? `${name} archived.` : p.kind === "block" ? `${name} blocked.` : `${name} unblocked.`);
      setPending(null);
      if (p.kind === "archive") onArchived?.();
    },
  });

  function ask(kind: NonNullable<Pending>["kind"], customer: CustomerSummary) {
    run.reset();
    setPending({ kind, customer });
  }

  const name = pending?.customer.name ?? (pending ? refOf(pending.customer) : "");
  const dialogs = (
    <ConfirmDialog
      open={pending !== null}
      onClose={() => setPending(null)}
      onConfirm={() => pending && run.mutate(pending)}
      busy={run.isPending}
      error={run.error ? (pending?.kind === "archive" ? archiveErrorText(run.error) : (run.error as Error).message) : null}
      danger={pending?.kind !== "unblock"}
      title={pending?.kind === "archive" ? "Archive customer" : pending?.kind === "block" ? "Block customer" : "Unblock customer"}
      confirmLabel={pending?.kind === "archive" ? "Archive" : pending?.kind === "block" ? "Block customer" : "Unblock"}
      message={
        pending?.kind === "archive" ? (
          <>
            Archive <strong className="text-ink">{name}</strong>? They leave the directory and any open new inquiry is cancelled
            with the reason &ldquo;customer archived&rdquo;. The record and its history stay in the audit log.
          </>
        ) : pending?.kind === "block" ? (
          <>
            Block <strong className="text-ink">{name}</strong>? Staff cannot open new trip cases for a blocked customer until an
            admin unblocks them.
          </>
        ) : (
          <>
            Unblock <strong className="text-ink">{name}</strong>? Staff can open trip cases for them again.
          </>
        )
      }
    />
  );

  return {
    canAdmin,
    block: (c: CustomerSummary) => ask("block", c),
    unblock: (c: CustomerSummary) => ask("unblock", c),
    archive: (c: CustomerSummary) => ask("archive", c),
    dialogs,
  };
}
