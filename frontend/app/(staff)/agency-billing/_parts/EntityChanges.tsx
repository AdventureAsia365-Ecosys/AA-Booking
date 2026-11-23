"use client";
// app/(staff)/agency-billing/_parts/EntityChanges.tsx — AA-1158 (bk-064): the payment details AA prints on agency
// invoices and the default issuing entity change only through a pending change a second person approves
// (billing_config.approve = admin, finance; never the requester, except the business owner, whose approval is flagged
// and listed in the audit log). Invoices keep the old details until the approval. The requester withdraws it.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { agencyBillingApi, type BillingEntity, type EntityChange } from "@/lib/api/agencyBilling";
import { api } from "@/lib/api/client";
import { Button, ConfirmDialog, DateText, Field, FormDrawer, Pill, Textarea, useCan, useStaff, useToast } from "@/ui";
import { errorText } from "./shared";
import { useT } from "@/ui/core/useI18n";
import { MSG } from "./EntityChanges.messages";

type Decision = { change: EntityChange; action: "approve" | "reject" | "withdraw" } | null;

/** The waiting changes of the entities, each with what it changes and the decision buttons the caller may use. */
export function PendingEntityChanges({ entities, onChanged }: { entities: BillingEntity[]; onChanged: () => void }) {
  const t = useT(MSG);
  const toast = useToast();
  const me = useStaff();
  const owner = useQuery({ queryKey: ["staff", "self-approval"], queryFn: api.staff.selfApproval, staleTime: 60_000 });
  const [decision, setDecision] = useState<Decision>(null);
  const run = useMutation({
    mutationFn: ({ d, reason }: { d: NonNullable<Decision>; reason: string }) =>
      agencyBillingApi.decideEntityChange(d.change.id, d.action, reason.trim()),
    onSuccess: (c) => {
      toast.success(t(DONE_TOAST[c.status] ?? "Change withdrawn"));
      setDecision(null);
      onChanged();
    },
  });
  const pending = entities.filter((e) => e.pending_change);
  if (pending.length === 0) return null;
  const action = decision?.action ?? "withdraw";
  const ownApproval = action === "approve" && decision?.change.requested_by === me.cis_user_id;
  return (
    <div className="mt-4 flex flex-col gap-3">
      {pending.map((e) => (
        <ChangeCard key={e.id} entity={e} me={me.cis_user_id} selfOk={!!owner.data?.approve} onDecide={setDecision} />
      ))}
      <ConfirmDialog
        open={decision !== null}
        onClose={() => setDecision(null)}
        onConfirm={(reason) => decision && run.mutate({ d: decision, reason: reason ?? "" })}
        busy={run.isPending}
        error={run.isError ? errorText(run.error) : null}
        danger={action === "reject"}
        title={t(DIALOG[action].title)}
        confirmLabel={t(DIALOG[action].confirm)}
        reason={action === "reject" ? { label: t("Reason"), required: true, maxLength: 300 } : undefined}
        message={t(ownApproval ? "You asked for this change. Approving it yourself is recorded as a business-owner self-approval." : DIALOG[action].message)}
      />
    </div>
  );
}

const DONE_TOAST: Record<string, "Change approved and applied" | "Change rejected" | "Change withdrawn"> = {
  approved: "Change approved and applied",
  rejected: "Change rejected",
  withdrawn: "Change withdrawn",
};

const DIALOG = {
  approve: {
    title: "Approve the change",
    confirm: "Approve",
    message: "The new details apply at once: invoices issued from now on print them.",
  },
  reject: { title: "Reject the change", confirm: "Reject", message: "The entity keeps its current details." },
  withdraw: { title: "Withdraw the change", confirm: "Withdraw", message: "The entity keeps its current details." },
} as const;

function ChangeCard({
  entity,
  me,
  selfOk,
  onDecide,
}: {
  entity: BillingEntity;
  me: string;
  selfOk: boolean;
  onDecide: (d: NonNullable<Decision>) => void;
}) {
  const t = useT(MSG);
  const c = entity.pending_change as EntityChange;
  const mine = c.requested_by === me;
  return (
    <div role="status" className="flex flex-col gap-2 rounded-lg border border-line bg-panel px-4 py-3 text-[13px] text-body">
      <div className="flex flex-wrap items-center gap-2">
        <Pill tone="warning">{t("Change pending")}</Pill>
        <span className="font-semibold">{entity.code}</span>
        <span className="text-muted">
          {t("asked by {0}", c.requested_by_name ?? c.requested_by)} · <DateText value={c.requested_at} />
        </span>
      </div>
      <p>{c.reason}</p>
      {c.set_payment_instructions && <DetailsDiff now={entity.payment_instructions} after={c.payment_instructions} />}
      {c.make_default && <p>{t("Makes this entity the default issuer of new invoices.")}</p>}
      <DecisionButtons change={c} mine={mine} selfOk={selfOk} onDecide={onDecide} />
      {mine && !selfOk && <p className="text-xs text-muted">{t("A second person with the approval right approves it.")}</p>}
    </div>
  );
}

function DetailsDiff({ now, after }: { now: string | null; after: string | null }) {
  const t = useT(MSG);
  return (
    <div className="grid gap-2 sm:grid-cols-2">
      <div>
        <div className="text-xs font-semibold uppercase tracking-wide text-muted">{t("Payment details now")}</div>
        <p className="whitespace-pre-line">{now ?? t("None")}</p>
      </div>
      <div>
        <div className="text-xs font-semibold uppercase tracking-wide text-muted">{t("Payment details after approval")}</div>
        <p className="whitespace-pre-line">{after ?? t("None")}</p>
      </div>
    </div>
  );
}

function DecisionButtons({
  change,
  mine,
  selfOk,
  onDecide,
}: {
  change: EntityChange;
  mine: boolean;
  selfOk: boolean;
  onDecide: (d: NonNullable<Decision>) => void;
}) {
  const t = useT(MSG);
  const canApprove = useCan("billing_config.approve");
  const canWrite = useCan("billing_config.write");
  const shown = [
    canApprove && (!mine || selfOk) ? ("approve" as const) : null,
    canApprove && !mine ? ("reject" as const) : null,
    canWrite && mine ? ("withdraw" as const) : null,
  ].filter((a): a is "approve" | "reject" | "withdraw" => a !== null);
  return (
    <div className="flex flex-wrap gap-2">
      {shown.map((action) => (
        <Button key={action} size="sm" variant={action === "approve" ? "primary" : "ghost"} onClick={() => onDecide({ change, action })}>
          {t(DIALOG[action].confirm)}
        </Button>
      ))}
    </div>
  );
}

/** Ask for new payment details on an entity (pending until approved). */
export function ChangeDetailsDrawer({ entity, onClose, onChanged }: { entity: BillingEntity | null; onClose: () => void; onChanged: () => void }) {
  const t = useT(MSG);
  const toast = useToast();
  const [text, setText] = useState("");
  const [reason, setReason] = useState("");
  const ask = useMutation({
    mutationFn: (e: BillingEntity) =>
      agencyBillingApi.requestEntityChange(e.id, {
        set_payment_instructions: true,
        payment_instructions: text.trim() || null,
        make_default: false,
        reason: reason.trim(),
      }),
    onSuccess: () => {
      toast.success(t("Change sent for approval"));
      setText("");
      setReason("");
      onChanged();
      onClose();
    },
  });
  return (
    <FormDrawer
      open={entity !== null}
      onClose={onClose}
      title={t("Change payment details")}
      onSubmit={() => entity && ask.mutate(entity)}
      busy={ask.isPending}
      error={ask.isError ? errorText(ask.error) : null}
    >
      {entity && (
        <div className="flex flex-col gap-3">
          <p className="text-[13px] text-muted">{t("The new details apply after a second person approves them; invoices keep the current details until then.")}</p>
          <Field label={t("New payment details")} hint={t("Printed on the invoice: where and how the agency pays")}>
            <Textarea value={text} onChange={(ev) => setText(ev.target.value)} />
          </Field>
          <Field label={t("Reason")} required>
            <Textarea value={reason} maxLength={300} onChange={(ev) => setReason(ev.target.value)} />
          </Field>
        </div>
      )}
    </FormDrawer>
  );
}

export function useEntityChangeInvalidate() {
  const qc = useQueryClient();
  return () => qc.invalidateQueries({ queryKey: ["agency-billing"] });
}
