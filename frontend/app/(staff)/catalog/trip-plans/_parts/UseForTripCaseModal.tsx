"use client";
// app/(staff)/catalog/trip-plans/_parts/UseForTripCaseModal.tsx — "Use for trip case" (UI spec §6.3, Figma
// TRIP TEMPLATE DETAILS-1; AA-679). POST /booking/catalog/trip-plans/{source_id}/trip-cases either
//   * creates a NEW trip case (existing customer or a new lead, start date, adults / children, currency) whose V1
//     lists the plan's CIS days as items, or
//   * adds a NEW VERSION built from the plan's days to an existing case (by its TC- code).
// Nothing is priced: the version starts unpriced, and sales add cost lines in the builder. Only sellable plans.

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/lib/api/client";
import type { CustomerSummary } from "@/lib/api/types";
import { tripPlansApi, type TripPlanDetail } from "@/lib/api/tripPlans";
import { Input, Select, Button, Modal, SegmentedControl, fieldName, useToast, DateField } from "@/ui";
import { CURRENCIES, Field, inputCls, paxErrors, toInt } from "../../../cases/_parts/fields";
import { CustomerPicker, EMPTY_LEAD, leadErrors, type LeadInput } from "../../../cases/new/CustomerPicker";

type Target = "new_case" | "existing_case";
const CASE_CODE_RE = /^TC-\d{6,}$/i; // lpad 6, grows past TC-999999 (bk-002 trip_case_code_seq)

export function UseForTripCaseModal({ plan, onClose }: { plan: TripPlanDetail; onClose: () => void }) {
  const router = useRouter();
  const toast = useToast();
  const qc = useQueryClient();
  const [target, setTarget] = useState<Target>("new_case");
  const [mode, setMode] = useState<"existing" | "new">("existing");
  const [customer, setCustomer] = useState<CustomerSummary | null>(null);
  const [lead, setLead] = useState<LeadInput>(EMPTY_LEAD);
  const [trip, setTrip] = useState({ start_date: "", pax_adults: "2", pax_children: "0", currency: "USD" });
  const [caseCode, setCaseCode] = useState("");
  const [tried, setTried] = useState(false);

  const pax = paxErrors(trip.pax_adults, trip.pax_children);
  const customerMissing = mode === "existing" && !customer;
  const leadInvalid = mode === "new" && Object.keys(leadErrors(lead)).length > 0;
  const codeInvalid = !CASE_CODE_RE.test(caseCode.trim());
  const invalid = target === "new_case" ? customerMissing || leadInvalid || !!pax.adults || !!pax.children : codeInvalid;
  const dayCount = plan.days.length || 1;

  const use = useMutation({
    mutationFn: () => {
      if (target === "existing_case") return tripPlansApi.useForTripCase(plan.source_id, { trip_case: caseCode.trim().toUpperCase() });
      const body: Record<string, unknown> = {
        currency: trip.currency,
        pax_adults: toInt(trip.pax_adults) ?? undefined,
        pax_children: toInt(trip.pax_children) ?? 0,
        start_date: trip.start_date || undefined,
      };
      if (mode === "existing") body.customer_id = customer?.id;
      else
        body.new_customer = {
          name: lead.name.trim(),
          email: lead.email.trim() || undefined,
          phone: lead.phone.trim() || undefined,
          contact_consent: lead.contact_consent,
        };
      return tripPlansApi.useForTripCase(plan.source_id, body);
    },
    onSuccess: (out) => {
      qc.invalidateQueries({ queryKey: ["trip-cases"] });
      qc.invalidateQueries({ queryKey: ["trip-plans", "detail", plan.source_id] });
      const code = out.trip_case.code;
      toast.success(
        out.mode === "new_case"
          ? `Trip case ${code} created with V1 from this plan`
          : `V${out.version.version_number} added to ${code} from this plan`,
      );
      router.push(`/cases/${encodeURIComponent(code)}?tab=proposals`);
    },
  });

  function submit() {
    setTried(true);
    if (invalid || use.isPending) return;
    use.mutate();
  }

  const err = use.error instanceof ApiError ? use.error : null;
  const fieldErrors = err?.fieldErrors ?? [];
  return (
    <Modal
      open
      onClose={() => !use.isPending && onClose()}
      title="Use for trip case"
      width={620}
      footer={
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose} disabled={use.isPending}>
            Cancel
          </Button>
          <Button variant="primary" onClick={submit} disabled={use.isPending}>
            {use.isPending ? "Creating…" : target === "new_case" ? "Create trip case" : "Add version"}
          </Button>
        </div>
      }
    >
      <form
        className="flex flex-col gap-4 font-sans"
        noValidate
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <p className="text-[13px] text-muted">
          The proposal starts with the {dayCount} {dayCount === 1 ? "item" : "day items"} of{" "}
          <span className="font-medium text-ink">{plan.title}</span>, titled from CIS. It is not priced: add cost lines and
          price it in the builder.
        </p>
        <SegmentedControl
          appearance="chips"
          label="Target"
          options={[
            { value: "new_case", label: "New trip case" },
            { value: "existing_case", label: "Add to an existing case" },
          ]}
          value={target}
          onChange={setTarget}
        />

        {target === "new_case" ? (
          <>
            <CustomerPicker
              mode={mode}
              onMode={setMode}
              customer={customer}
              onCustomer={setCustomer}
              lead={lead}
              onLead={setLead}
              showErrors={tried && (customerMissing || leadInvalid)}
            />
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <Field label="Start date" hint="Optional">
                <DateField className={inputCls} value={trip.start_date} onChange={(v) => setTrip({ ...trip, start_date: v })} />
              </Field>
              <Field label="Currency">
                <Select className={inputCls} value={trip.currency} onChange={(e) => setTrip({ ...trip, currency: e.target.value })}>
                  {CURRENCIES.map((c) => (
                    <option key={c} value={c}>
                      {c}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field label="Adults" required error={tried ? pax.adults : null}>
                <Input className={inputCls} type="number" inputMode="numeric" min={1} max={999} value={trip.pax_adults} onChange={(e) => setTrip({ ...trip, pax_adults: e.target.value })} />
              </Field>
              <Field label="Children" error={tried ? pax.children : null}>
                <Input className={inputCls} type="number" inputMode="numeric" min={0} max={998} value={trip.pax_children} onChange={(e) => setTrip({ ...trip, pax_children: e.target.value })} />
              </Field>
            </div>
          </>
        ) : (
          <Field
            label="Trip case code"
            required
            hint="The plan's days become a new version of that case; its earlier versions stay."
            error={tried && codeInvalid ? "Enter a case code like TC-000123." : null}
          >
            <Input className={inputCls} placeholder="TC-000123" value={caseCode} onChange={(e) => setCaseCode(e.target.value)} />
          </Field>
        )}

        {err && (
          <div role="alert" className="rounded-md border border-danger-border bg-danger-soft px-3 py-2 text-[13px] text-danger">
            {fieldErrors.length ? (
              <ul className="list-disc pl-4">
                {fieldErrors.map((f, i) => (
                  <li key={i}>
                    <span className="font-medium">{fieldName(f) || "form"}</span>: {f.msg}
                  </li>
                ))}
              </ul>
            ) : err.code === "trip_case_not_found" ? (
              <p>No open trip case has that code.</p>
            ) : err.code === "trip_case_closed" ? (
              <p>That trip case is closed (completed or cancelled). Reopen it first, or create a new case.</p>
            ) : (
              <p>{err.message}</p>
            )}
          </div>
        )}
        <button type="submit" hidden aria-hidden="true" tabIndex={-1} />
      </form>
    </Modal>
  );
}
