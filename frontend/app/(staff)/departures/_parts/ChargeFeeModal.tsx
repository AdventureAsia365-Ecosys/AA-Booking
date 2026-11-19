"use client";
// app/(staff)/departures/_parts/ChargeFeeModal.tsx — "Charge fee" (AA-1073 rest, bk-048; S275 study item bk-09): propose
// a departure fee to one agency (minimum not reached, allotted seats not released, or other with a note). The fee waits
// for a second person (b2b_desk.approve); once approved the agency is told (event departure.fee_charged) and the
// settlement draft bills it. Opened from the departure board drawer, the at-risk rows of the B2B desk and an agency
// allotment on a trip plan's departure; it reads the departure's agencies and the prefill from the board detail.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/lib/api/client";
import { FEE_REASON_LABEL, departureBoardApi, type FeeReason } from "@/lib/api/departureBoard";
import { Button, Input, Modal, Select, Textarea, useToast } from "@/ui";
import { FormField, errorFor, inputCls } from "../../_parts/FormField";
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { useBookingFormat } from "@/ui/local/useBookingFormat";
import { MSG } from "./ChargeFeeModal.messages";

const REASONS: FeeReason[] = ["min_shortfall", "allotment_unreleased", "other"];

function errorText(e: unknown): string {
  return e instanceof ApiError || e instanceof Error ? e.message : "Something went wrong";
}

export function ChargeFeeModal({
  departureId,
  title,
  reason: initialReason = "min_shortfall",
  partnerId: initialPartner = "",
  seats: initialSeats,
  onClose,
}: {
  departureId: string;
  /** "Departure 14 Dec 2026 · tour" */
  title: string;
  reason?: FeeReason;
  partnerId?: string;
  seats?: number;
  onClose: () => void;
}) {
  const t = useT(MSG);
  const { formatMoney } = useBookingFormat();
  const tx = useTx();
  const qc = useQueryClient();
  const toast = useToast();
  const detail = useQuery({ queryKey: ["departure-board", "detail", departureId], queryFn: () => departureBoardApi.detail(departureId) });
  const suggested = detail.data?.fee_suggestions.find((s) => s.reason === initialReason && (!initialPartner || s.partner_id === initialPartner));
  const [reason, setReason] = useState<FeeReason>(initialReason);
  const [partner, setPartner] = useState(initialPartner);
  const [seats, setSeats] = useState<string>(initialSeats ? String(initialSeats) : "");
  const [price, setPrice] = useState("");
  const [currency, setCurrency] = useState("USD");
  const [note, setNote] = useState("");
  const seatValue = seats || (suggested ? String(suggested.seats) : "");
  const propose = useMutation({
    mutationFn: () =>
      departureBoardApi.proposeFee(departureId, {
        partner_id: partner,
        reason,
        seats: Number.parseInt(seatValue, 10) || 0,
        price_per_seat: price,
        currency: currency.trim().toUpperCase(),
        note: note.trim() || null,
      }),
    onSuccess: (fee) => {
      toast.success(t("Fee of {0} {1} proposed; another person approves it", fee.amount, fee.currency));
      qc.invalidateQueries({ queryKey: ["departure-board"] });
      qc.invalidateQueries({ queryKey: ["b2b-desk"] });
      onClose();
    },
  });
  const errors = propose.error instanceof ApiError ? propose.error.fieldErrors : [];
  const total = (Number.parseInt(seatValue, 10) || 0) * (Number.parseFloat(price) || 0);
  return (
    <Modal
      open
      onClose={onClose}
      title={t("Charge departure fee")}
      footer={
        <>
          <Button onClick={onClose}>{t("Cancel")}</Button>
          <Button variant="primary" disabled={!partner || !price || !seatValue || propose.isPending} onClick={() => propose.mutate()}>
            {propose.isPending ? t("Proposing…") : t("Propose fee")}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-4 text-[13px] text-body" data-testid="charge-fee">
        <p>
          {t("{0}. The fee waits for a second person to approve it; the agency is then told and it goes on its next settlement invoice.", title)}
        </p>
        <FormField label={t("Agency")} required error={errorFor(errors, "partner_id")}>
          <Select className={inputCls} value={partner} onChange={(e) => setPartner(e.target.value)}>
            <option value="">{detail.isLoading ? t("Loading…") : t("Choose an agency")}</option>
            {(detail.data?.agencies ?? []).map((a) => (
              <option key={a.partner_id} value={a.partner_id}>
                {t("{0} (on this departure)", a.agency_name)}
              </option>
            ))}
            {(detail.data?.partner_options ?? [])
              .filter((p) => !(detail.data?.agencies ?? []).some((a) => a.partner_id === p.id))
              .map((p) => (
                <option key={p.id} value={p.id}>
                  {p.agency_name}
                </option>
              ))}
          </Select>
        </FormField>
        <FormField label={t("Reason")} required error={errorFor(errors, "reason")}>
          <Select className={inputCls} value={reason} onChange={(e) => setReason(e.target.value as FeeReason)}>
            {REASONS.map((r) => (
              <option key={r} value={r}>
                {tx(FEE_REASON_LABEL[r])}
              </option>
            ))}
          </Select>
        </FormField>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <FormField label={t("Seats")} required error={errorFor(errors, "seats")}>
            <Input className={inputCls} type="number" min={1} max={1000} value={seatValue} onChange={(e) => setSeats(e.target.value)} />
          </FormField>
          <FormField label={t("Price per seat")} required error={errorFor(errors, "price_per_seat")}>
            <Input className={inputCls} inputMode="decimal" value={price} placeholder="0.00" onChange={(e) => setPrice(e.target.value)} />
          </FormField>
          <FormField label={t("Currency")} required error={errorFor(errors, "currency")}>
            <Input className={inputCls} maxLength={3} value={currency} onChange={(e) => setCurrency(e.target.value.toUpperCase())} />
          </FormField>
        </div>
        <FormField label={t("Note")} required={reason === "other"} hint={t("Why the fee is charged. Never a traveller's name.")} error={errorFor(errors, "note")}>
          <Textarea className={inputCls} rows={2} maxLength={300} value={note} onChange={(e) => setNote(e.target.value)} />
        </FormField>
        <p className="bk-tabular text-[13px] font-medium text-ink">
          {t("Total {0} {1}", formatMoney(total > 0 ? total : 0, null), currency || "USD")}
        </p>
        {propose.isError && !errors.length && (
          <p role="alert" className="text-danger">
            {tx(errorText(propose.error))}
          </p>
        )}
      </div>
    </Modal>
  );
}
