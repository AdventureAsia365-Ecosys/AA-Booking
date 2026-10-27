"use client";
// app/(staff)/proposals/_parts/SendProposalModal.tsx
// AA-683 — "Send to customer" (UI spec §4.9 send modal; Figma ITINERARY BUILDER-2 with the primary button
// labelled "Send", not "Create Invoice"). POST …/versions/{n}/send marks the version sent, locks it, moves
// the case to proposal_sent and writes a customer notification row. No e-mail is sent today (CONTEXT.md,
// Q21), so nothing here says one was: the copy says "marked as sent".
//
// Used by the builder, the preview and the /proposals row menu; it loads the case and the version itself.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Lock, Send } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import { api } from "@/lib/api/client";
import { proposalsApi } from "@/lib/api/proposals";
import { QUOTE_VALIDITY_RANGE_DAYS, settingsFromApi } from "@/lib/bookingConfig";
import { addDays, defaultSendMessage } from "@/lib/proposalDraft";
import { Button, Modal, Money, Spinner, formatDate, staffDisplay, useStaff, useToast } from "@/ui";
import { DateField } from "@/ui/booking/DateField";

const SENDABLE_FROM = ["discovery", "building", "proposal_sent"];
const fieldCls = "w-full rounded-md border border-line bg-card px-3 text-sm text-ink outline-none focus:border-accent-border";

export function SendProposalModal({
  open,
  onClose,
  code,
  n,
  onSent,
}: {
  open: boolean;
  onClose: () => void;
  code: string;
  n: number;
  onSent?: () => void;
}) {
  return (
    <Modal open={open} onClose={onClose} title="Send proposal to customer" width={560}>
      {open && <SendBody code={code} n={n} onClose={onClose} onSent={onSent} />}
    </Modal>
  );
}

function SendBody({ code, n, onClose, onSent }: { code: string; n: number; onClose: () => void; onSent?: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const me = useStaff();
  const tripCase = useQuery({ queryKey: ["trip-case", code], queryFn: () => api.tripCases.get(code) });
  const version = useQuery({ queryKey: ["trip-case", code, "version", n], queryFn: () => api.versions.items(code, n) });
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.reference.settings, staleTime: 10 * 60_000 });

  if (tripCase.isLoading || version.isLoading) {
    return (
      <div className="flex items-center gap-2 py-8 text-[13px] text-muted">
        <Spinner /> Loading the proposal…
      </div>
    );
  }
  if (!tripCase.data || !version.data) {
    return <p className="text-[13px] text-danger">{((tripCase.error ?? version.error) as Error | null)?.message ?? "Could not load the proposal."}</p>;
  }
  const c = tripCase.data;
  const v = version.data;
  const validityDays = settingsFromApi(settings.data?.values).quoteValidityDays;
  const title = v.title ?? c.trip_title ?? "custom";
  const blockers: string[] = [];
  if (v.is_sent_to_customer || v.is_approved) blockers.push("This version was already sent. Create a new version to send changes.");
  if (!SENDABLE_FROM.includes(c.status)) blockers.push(`A proposal can be sent while the case is in discovery, building or proposal sent; it is ${c.status.replace(/_/g, " ")}.`);
  if (v.items.length === 0) blockers.push("Add at least one item before sending.");
  if (v.total_price == null || !v.currency) blockers.push("The version has no total price yet. Apply a price in the builder's Price panel first.");

  return (
    <SendForm
      key={`${c.code}-${v.version_number}`}
      blockers={blockers}
      summary={{
        title,
        customer: c.customer_name,
        dates: c.start_date ? `${formatDate(c.start_date)}${c.trip_end_date ? ` – ${formatDate(c.trip_end_date)}` : ""}` : "Dates to be confirmed",
        total: v.total_price,
        currency: v.currency,
      }}
      to={c.customer_email}
      defaults={{
        subject: `Your Adventure Asia proposal: ${title}`.slice(0, 200),
        message: defaultSendMessage({ customerName: c.customer_name, tripTitle: title, staffName: staffDisplay(me).name }),
        validUntil: addDays(new Date(), validityDays),
      }}
      onCancel={onClose}
      onSubmit={async (body) => {
        const sent = await proposalsApi.send(c.code, v.version_number, body);
        await Promise.all([
          qc.invalidateQueries({ queryKey: ["trip-case", code] }),
          qc.invalidateQueries({ queryKey: ["trip-case", c.code] }),
          qc.invalidateQueries({ queryKey: ["versions"] }),
        ]);
        toast.success(`${c.code} V${v.version_number} marked as sent and locked. No e-mail was sent.`, 6000);
        onSent?.();
        onClose();
        return sent;
      }}
    />
  );
}

function SendForm({
  blockers,
  summary,
  to,
  defaults,
  onCancel,
  onSubmit,
}: {
  blockers: string[];
  summary: { title: string; customer: string | null; dates: string; total: string | null; currency: string | null };
  to: string | null;
  defaults: { subject: string; message: string; validUntil: string };
  onCancel: () => void;
  onSubmit: (body: { to?: string; subject: string; message: string; valid_until: string }) => Promise<unknown>;
}) {
  const [subject, setSubject] = useState(defaults.subject);
  const [message, setMessage] = useState(defaults.message);
  const [validUntil, setValidUntil] = useState(defaults.validUntil);
  const min = addDays(new Date(), QUOTE_VALIDITY_RANGE_DAYS.min);
  const max = addDays(new Date(), QUOTE_VALIDITY_RANGE_DAYS.max);
  const send = useMutation({
    mutationFn: () => onSubmit({ ...(to ? { to } : {}), subject: subject.trim(), message: message.trim(), valid_until: validUntil }),
  });
  const err = send.error as ApiError | null;
  const invalid = !subject.trim() || !message.trim() || !validUntil || validUntil < min || validUntil > max;

  return (
    <div className="flex flex-col gap-4 font-sans">
      <div className="rounded-md border border-line bg-panel px-4 py-3">
        <p className="text-sm font-semibold text-ink">{summary.title}</p>
        <p className="mt-0.5 text-[13px] text-muted">
          Customer: {summary.customer ?? "unnamed"} · {summary.dates}
        </p>
        <p className="mt-1 text-[13px] text-muted">
          Total <Money amount={summary.total} currency={summary.currency} strong /> · locked when sent
        </p>
      </div>

      {blockers.length > 0 ? (
        <div role="alert" className="rounded-md border border-danger-border bg-danger-soft px-3 py-2 text-[13px] text-danger">
          <p className="font-semibold">This version cannot be sent yet</p>
          <ul className="mt-1 list-disc pl-4 text-body">
            {blockers.map((b) => (
              <li key={b}>{b}</li>
            ))}
          </ul>
        </div>
      ) : (
        <>
          <label className="flex flex-col gap-1.5">
            <span className="text-[12px] font-medium uppercase tracking-[0.04em] text-muted">To</span>
            <span className="flex h-10 items-center gap-2 rounded-md border border-line bg-panel px-3 text-sm text-ink">
              <Lock size={13} className="text-muted2" /> {to ?? "No e-mail on file for this customer"}
            </span>
            <span className="text-xs text-muted">Always the customer&apos;s own e-mail; change it on the customer record.</span>
          </label>
          <label className="flex flex-col gap-1.5">
            <span className="text-[12px] font-medium uppercase tracking-[0.04em] text-muted">Subject</span>
            <input value={subject} maxLength={200} onChange={(e) => setSubject(e.target.value)} className={`${fieldCls} h-10`} />
          </label>
          <label className="flex flex-col gap-1.5">
            <span className="text-[12px] font-medium uppercase tracking-[0.04em] text-muted">Message</span>
            <textarea value={message} rows={8} maxLength={10_000} onChange={(e) => setMessage(e.target.value)} className={`${fieldCls} py-2 leading-relaxed`} />
          </label>
          <label className="flex flex-col gap-1.5">
            <span className="text-[12px] font-medium uppercase tracking-[0.04em] text-muted">Valid until</span>
            <DateField value={validUntil} min={min} max={max} onChange={(v) => setValidUntil(v)} className={`${fieldCls} h-10`} wrapperClassName="sm:w-56" />
            <span className="text-xs text-muted">
              {QUOTE_VALIDITY_RANGE_DAYS.min}–{QUOTE_VALIDITY_RANGE_DAYS.max} days from today.
            </span>
          </label>
          <p className="rounded-md bg-accent-tint px-3 py-2 text-[13px] text-body">
            Sending marks this version as sent, locks its items and price, and moves the case to Proposal sent. Booking does not
            send e-mail yet: share the proposal with the customer yourself.
          </p>
        </>
      )}

      {err && (
        <p role="alert" className="text-[13px] text-danger">
          {err.fieldErrors.length ? err.fieldErrors.map((f) => f.msg).join(" ") : err.message}
        </p>
      )}
      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={onCancel} disabled={send.isPending}>
          Cancel
        </Button>
        <Button variant="primary" onClick={() => send.mutate()} disabled={blockers.length > 0 || invalid || send.isPending}>
          <Send size={14} /> {send.isPending ? "Sending…" : "Send"}
        </Button>
      </div>
    </div>
  );
}
