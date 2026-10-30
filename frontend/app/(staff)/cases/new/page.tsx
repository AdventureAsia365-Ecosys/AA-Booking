"use client";
// app/(staff)/cases/new/page.tsx — Create trip case (W1, UI spec §4.6; Figma ADD NEW TRIP 8, enriched per
// 03 decision 4). POST /booking/trip-cases with an existing customer or an inline new lead, start date,
// adults / children, currency, countries, initial status, sales rep and internal notes. Trip style is
// left out until its source is decided (Q16).
// AA-996 (Figma audit slice 2): "Start from a trip plan" (optional) — with a sellable plan picked, the case is created
// by POST /booking/catalog/trip-plans/{source_id}/trip-cases (same fields), so V1 starts from the plan's CIS days, the
// case keeps the plan's key and countries default to the plan's country.

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";
import { api, ApiError } from "@/lib/api/client";
import { tripPlansApi, type TripPlanListItem } from "@/lib/api/tripPlans";
import type { CustomerSummary } from "@/lib/api/types";
import { Input, Select, Textarea, PageFrame, Button, Section, useCan, useStaff, useToast, fieldName, DateField } from "@/ui";
import { CURRENCIES, Field, inputCls, paxErrors, textareaCls, toInt } from "../_parts/fields";
import { CountryPicker } from "../_parts/CountryPicker";
import { StaffSelect } from "../_parts/StaffSelect";
import { QueryError } from "../_parts/QueryError";
import { CustomerPicker, EMPTY_LEAD, leadErrors, type LeadInput } from "./CustomerPicker";
import { TripPlanPicker } from "../_parts/TripPlanPicker";

export default function NewTripCasePage() {
  const router = useRouter();
  const toast = useToast();
  const qc = useQueryClient();
  const me = useStaff();
  const canWrite = useCan("trip_case.write");
  const canStartFromPlan = useCan("version.write");
  const [plan, setPlan] = useState<TripPlanListItem | null>(null);

  const [mode, setMode] = useState<"existing" | "new">("existing");
  const [customer, setCustomer] = useState<CustomerSummary | null>(null);
  const [lead, setLead] = useState<LeadInput>(EMPTY_LEAD);
  const [trip, setTrip] = useState({ start_date: "", pax_adults: "2", pax_children: "0", currency: "USD" });
  const [countries, setCountries] = useState<string[]>([]);
  const [initialStatus, setInitialStatus] = useState<"new_inquiry" | "discovery">("new_inquiry");
  const [assignee, setAssignee] = useState(me.role === "sales" ? me.cis_user_id : "");
  const [notes, setNotes] = useState("");
  const [tried, setTried] = useState(false);

  const pax = paxErrors(trip.pax_adults, trip.pax_children);
  const customerMissing = mode === "existing" && !customer;
  const leadInvalid = mode === "new" && Object.keys(leadErrors(lead)).length > 0;
  const invalid = customerMissing || leadInvalid || !!pax.adults || !!pax.children;

  const create = useMutation({
    mutationFn: () => {
      const body: Record<string, unknown> = {
        currency: trip.currency,
        pax_adults: toInt(trip.pax_adults) ?? undefined,
        pax_children: toInt(trip.pax_children) ?? 0,
        start_date: trip.start_date || undefined,
        countries,
        initial_status: initialStatus,
        notes: notes.trim() || undefined,
        assignee: assignee || undefined,
      };
      if (mode === "existing") body.customer_id = customer?.id;
      else
        body.new_customer = {
          name: lead.name.trim(),
          email: lead.email.trim() || undefined,
          phone: lead.phone.trim() || undefined,
          contact_consent: lead.contact_consent,
        };
      if (plan) return tripPlansApi.useForTripCase(plan.source_id, body).then((r) => r.trip_case);
      return api.tripCases.create(body);
    },
    onSuccess: (c) => {
      qc.invalidateQueries({ queryKey: ["trip-cases"] });
      toast.success(`Trip case ${c.code} created`);
      router.push(`/cases/${encodeURIComponent(c.code)}`);
    },
  });

  function submit() {
    setTried(true);
    if (invalid || create.isPending) return;
    create.mutate();
  }

  const apiError = create.error instanceof ApiError ? create.error : null;

  if (!canWrite) {
    return (
      <PageFrame width="full">
        <QueryError error={new ApiError("forbidden", 403)} what="creating trip cases" />
      </PageFrame>
    );
  }

  return (
    <PageFrame bleed>
      {/* Header bar (Figma ADD NEW TRIP 8): back, title, Cancel + primary. */}
      <div className="border-b border-line bg-bg px-4 py-4 sm:px-8">
        <div className="mx-auto flex max-w-[880px] flex-wrap items-center justify-between gap-3">
          <div className="flex min-w-0 items-center gap-3">
            <Link href="/cases" aria-label="Back to trip cases" className="grid h-8 w-8 shrink-0 place-items-center rounded-md border border-line bg-card text-muted hover:text-ink">
              <ArrowLeft size={16} />
            </Link>
            <h1 className="truncate font-display text-xl font-semibold text-ink">Create new trip case</h1>
          </div>
          <div className="flex items-center gap-2">
            <Link href="/cases" className="px-3 text-[13px] font-medium text-muted hover:text-ink">
              Cancel
            </Link>
            <Button variant="primary" onClick={submit} disabled={create.isPending}>
              {create.isPending ? "Creating…" : "Create trip case"}
            </Button>
          </div>
        </div>
      </div>

      <form
        className="mx-auto flex max-w-[880px] flex-col gap-5 px-4 py-6 sm:px-8"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
        noValidate
      >
        <p className="text-[13px] text-muted">
          For a customer who reached us outside TripPlanner. TripPlanner inquiries open their case on their own.
        </p>

        <Section title="Customer information" description="Search the customer directory, or add a new lead.">
          <CustomerPicker
            mode={mode}
            onMode={setMode}
            customer={customer}
            onCustomer={setCustomer}
            lead={lead}
            onLead={setLead}
            showErrors={tried && (customerMissing || leadInvalid)}
          />
        </Section>

        {canStartFromPlan && (
          <Section
            title="Start from a trip plan"
            description="Optional. Pick a sellable plan: proposal V1 starts from its CIS days (unpriced), and the case keeps the link to the plan."
          >
            <TripPlanPicker value={plan} onChange={setPlan} disabled={create.isPending} />
          </Section>
        )}

        <Section title="Trip details" description="The rough shape of the trip. Everything here can change later in the case.">
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Field label="Start date" hint="Optional">
              <DateField className={inputCls} value={trip.start_date} onChange={(v) => setTrip({ ...trip, start_date: v })} />
            </Field>
            <Field label="Adults" required error={tried ? pax.adults : null}>
              <Input className={inputCls} type="number" inputMode="numeric" min={1} max={999} value={trip.pax_adults} onChange={(e) => setTrip({ ...trip, pax_adults: e.target.value })} />
            </Field>
            <Field label="Children" error={tried ? pax.children : null}>
              <Input className={inputCls} type="number" inputMode="numeric" min={0} max={998} value={trip.pax_children} onChange={(e) => setTrip({ ...trip, pax_children: e.target.value })} />
            </Field>
            <Field label="Currency">
              <Select className={inputCls} value={trip.currency} onChange={(e) => setTrip({ ...trip, currency: e.target.value })}>
                {CURRENCIES.map((c) => (
                  <option key={c}>{c}</option>
                ))}
              </Select>
            </Field>
            <Field
              label="Countries"
              hint={plan && !countries.length ? "Optional. Left empty, the plan's country is used." : "Optional. The order you pick is kept."}
              className="sm:col-span-2 lg:col-span-4"
            >
              <CountryPicker value={countries} onChange={setCountries} />
            </Field>
          </div>
        </Section>

        <Section title="Assignment and notes">
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Sales rep" hint={me.role === "sales" ? "Defaults to you." : undefined}>
              <StaffSelect value={assignee} onChange={setAssignee} />
            </Field>
            <Field label="Initial status" hint="Use Discovery when you are already talking with the customer.">
              <Select className={inputCls} value={initialStatus} onChange={(e) => setInitialStatus(e.target.value as "new_inquiry" | "discovery")}>
                <option value="new_inquiry">New inquiry</option>
                <option value="discovery">Discovery</option>
              </Select>
            </Field>
            <Field label="Internal notes" hint="Staff only. The customer never sees these." className="sm:col-span-2">
              <Textarea
                className={textareaCls}
                value={notes}
                maxLength={10000}
                placeholder="What the customer asked for, how they reached us, anything the next person should know."
                onChange={(e) => setNotes(e.target.value)}
              />
            </Field>
          </div>
        </Section>

        {(create.isError || (tried && invalid)) && (
          <div role="alert" className="rounded-md border border-danger-border bg-danger-soft px-4 py-3 text-[13px] text-danger">
            {create.isError ? (
              <>
                <p>{(create.error as Error).message}</p>
                {apiError?.fieldErrors.map((f, i) => (
                  <p key={i}>
                    {fieldName(f) || "form"}: {f.msg}
                  </p>
                ))}
              </>
            ) : (
              <p>Some fields need attention before the case can be created.</p>
            )}
          </div>
        )}

        {/* Phone: the header buttons scroll away, so the primary action repeats at the end of the form. */}
        <div className="flex justify-end gap-2 sm:hidden">
          <Button variant="primary" type="submit" disabled={create.isPending}>
            {create.isPending ? "Creating…" : "Create trip case"}
          </Button>
        </div>
      </form>
    </PageFrame>
  );
}
