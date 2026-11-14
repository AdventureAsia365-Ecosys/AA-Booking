"use client";

// app/(staff)/pricing/_shared/CloseRow.tsx — S280 (AA-1096, bk-058): Reject / Withdraw of a pending pricing rule, FX
// rate or cancellation fee schedule, and the line that says who closed a row. The buttons follow the API's per-row
// `can_reject` (a second person, with a reason) and `can_withdraw` (the maker); the API decides (403 own_row /
// not_maker, 409 already_approved / already_decided). Used by Pricing rules & FX and Pricing › Policy › Cancellation
// fees; a closed row keeps its day (the unique day of a row), so the dialogs say to enter the correction on another day.

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/lib/api/client";
import { pricingPolicyApi, type ClosableKind, type ClosableRow } from "@/lib/api/pricingPolicy";
import { Button, ConfirmDialog, useT, useToast } from "@/ui";
import { C, type CKey } from "./closeRow.messages";

type CT = ReturnType<typeof useT<typeof C.en>>;

const CODES: Record<string, CKey> = {
  own_row: "errOwnRow",
  not_maker: "errNotMaker",
  already_approved: "errAlreadyApproved",
  already_decided: "errAlreadyDecided",
  reason_required: "errReason",
  rule_not_found: "errGone",
  fx_rate_not_found: "errGone",
  cancellation_policy_not_found: "errGone",
};
const REFRESH = new Set(["already_approved", "already_decided", "rule_not_found", "fx_rate_not_found", "cancellation_policy_not_found"]);

function errorText(t: CT, err: unknown): string {
  if (!(err instanceof ApiError)) return err instanceof Error ? err.message : String(err);
  const key = CODES[err.code];
  if (key) return t(key) as string;
  if (err.fieldErrors.length) return t("errReason") as string;
  return t("errGeneric", err.code || String(err.status)) as string;
}

type Step = "reject" | "withdraw";

/** Reject / Withdraw buttons of one pending row; `what` names the row in the dialog ("EUR rate from 2026-11-20"). */
export function CloseRowButtons({ kind, row, what }: { kind: ClosableKind; row: ClosableRow; what: string }) {
  const t = useT(C);
  const qc = useQueryClient();
  const toast = useToast();
  const [step, setStep] = useState<Step | null>(null);
  const close = useMutation({
    mutationFn: ({ s, reason }: { s: Step; reason: string }) =>
      s === "reject" ? pricingPolicyApi.rejectRow(kind, row.id, reason) : pricingPolicyApi.withdrawRow(kind, row.id),
    onSuccess: (_, { s }) => {
      qc.invalidateQueries({ queryKey: ["pricing"] });
      toast.success(t(s === "reject" ? "rejected" : "withdrawn") as string);
      setStep(null);
    },
    onError: (err) => {
      if (err instanceof ApiError && REFRESH.has(err.code)) qc.invalidateQueries({ queryKey: ["pricing"] });
      toast.error(errorText(t, err));
    },
  });
  if (!row.can_reject && !row.can_withdraw) return null;
  const rejecting = step === "reject";
  return (
    <span className="flex flex-wrap items-center gap-2" onClick={(e) => e.stopPropagation()}>
      {row.can_reject && (
        <Button size="sm" variant="secondary" onClick={() => setStep("reject")} title={t("rejectHint") as string}>
          {t("reject")}
        </Button>
      )}
      {row.can_withdraw && (
        <Button size="sm" variant="secondary" onClick={() => setStep("withdraw")} title={t("withdrawHint") as string}>
          {t("withdraw")}
        </Button>
      )}
      <ConfirmDialog
        open={step !== null}
        onClose={() => setStep(null)}
        busy={close.isPending}
        destructive
        title={t(rejecting ? "rejectTitle" : "withdrawTitle") as string}
        body={t(rejecting ? "rejectBody" : "withdrawBody", what) as string}
        confirmLabel={t(rejecting ? "reject" : "withdraw") as string}
        reason={rejecting ? { label: t("rejectReason") as string, required: true, maxLength: 300 } : undefined}
        onConfirm={(reason) => step && close.mutate({ s: step, reason })}
      />
    </span>
  );
}

/** "Rejected by <name>: <reason>" / "Withdrawn by <name>" under a closed row; nothing for any other row. */
export function ClosedNote({ row }: { row: ClosableRow }) {
  const t = useT(C);
  if (row.status !== "rejected" && row.status !== "withdrawn") return null;
  const who = row.closed_by ? (row.closed_by.name ?? row.closed_by.id) : "—";
  const text = row.status === "rejected" ? t("closedRejected", who, row.close_reason ?? "") : t("closedWithdrawn", who);
  return <span className="block max-w-[260px] break-words text-xs leading-snug text-muted">{text as string}</span>;
}
