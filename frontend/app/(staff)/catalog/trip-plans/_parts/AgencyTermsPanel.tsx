"use client";
// app/(staff)/catalog/trip-plans/_parts/AgencyTermsPanel.tsx — the tour's agency terms (AA-1075 bk-15, bk-18).
//
// - Age bands (trip_plan.write): the oldest age at the child price and at the infant price (empty = the defaults 11
//   and 1). The prices themselves come from the b2b pricing rule (child share, infant net) and the price matrix.
// - Deposit and balance (agency_billing.read; billing_config.write to change): the tour's own deposit % and balance
//   days, for every agency or one agency. The most specific wins on the settlement drafts and the agency's
//   GET /terms?source_id=: agency + tour > tour > the agency's terms > AA's default.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Trash2 } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import { agencyOpsApi, type TourTerms } from "@/lib/api/agencyOps";
import type { TripPlanDetail } from "@/lib/api/tripPlans";
import { Button, Field, Input, Section, Select, useCan, useToast } from "@/ui";

function errorText(e: unknown): string {
  return e instanceof ApiError || e instanceof Error ? e.message : "Something went wrong";
}

function pctText(fraction: string): string {
  return `${Math.round(Number(fraction) * 10000) / 100} %`;
}

export function AgencyTermsPanel({ plan }: { plan: TripPlanDetail }) {
  const canAges = useCan("trip_plan.write");
  const canTerms = useCan("agency_billing.read");
  if (!canAges && !canTerms) return null;
  return (
    <Section
      title="Agency terms"
      description="Child and infant ages of this tour, and its own deposit and balance terms for agencies."
    >
      <div className="flex flex-col gap-6">
        <AgeBands plan={plan} canEdit={canAges} />
        {canTerms && <TourTermsList sourceId={plan.source_id} />}
      </div>
    </Section>
  );
}

function AgeBands({ plan, canEdit }: { plan: TripPlanDetail; canEdit: boolean }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [child, setChild] = useState(plan.child_age_max != null ? String(plan.child_age_max) : "");
  const [infant, setInfant] = useState(plan.infant_age_max != null ? String(plan.infant_age_max) : "");
  const save = useMutation({
    mutationFn: () =>
      agencyOpsApi.ageBands(plan.source_id, {
        child_age_max: child.trim() ? Number(child) : null,
        infant_age_max: infant.trim() ? Number(infant) : null,
      }),
    onSuccess: () => {
      toast.success("Age bands saved");
      qc.invalidateQueries({ queryKey: ["trip-plans"] });
    },
    onError: (e) => toast.error(errorText(e)),
  });
  return (
    <div>
      <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted">Age bands</p>
      <div className="flex flex-wrap items-end gap-3">
        <Field label="Child up to age" hint="Empty = 11">
          <Input type="number" min={2} max={17} value={child} disabled={!canEdit} onChange={(e) => setChild(e.target.value)} style={{ width: 120 }} />
        </Field>
        <Field label="Infant up to age" hint="Empty = 1">
          <Input type="number" min={0} max={4} value={infant} disabled={!canEdit} onChange={(e) => setInfant(e.target.value)} style={{ width: 120 }} />
        </Field>
        {canEdit && (
          <Button size="sm" variant="ghost" disabled={save.isPending} onClick={() => save.mutate()}>
            Save ages
          </Button>
        )}
      </div>
    </div>
  );
}

function TourTermsList({ sourceId }: { sourceId: string }) {
  const canWrite = useCan("billing_config.write");
  const qc = useQueryClient();
  const toast = useToast();
  const key = ["tour-terms", sourceId];
  const q = useQuery({ queryKey: key, queryFn: () => agencyOpsApi.tourTerms(sourceId) });
  const agencies = useQuery({ queryKey: ["b2b-desk", "agencies"], queryFn: agencyOpsApi.agencies, enabled: canWrite, retry: false });
  const [tenant, setTenant] = useState("");
  const [deposit, setDeposit] = useState("30");
  const [days, setDays] = useState("30");
  const refresh = () => qc.invalidateQueries({ queryKey: key });
  const put = useMutation({
    mutationFn: () =>
      agencyOpsApi.putTourTerms(sourceId, {
        tenant_id: tenant || null,
        deposit_pct: (Number(deposit) / 100).toFixed(4),
        balance_days_before_departure: Number(days),
      }),
    onSuccess: () => {
      toast.success("Tour terms saved");
      refresh();
    },
    onError: (e) => toast.error(errorText(e)),
  });
  const remove = useMutation({
    mutationFn: (t: TourTerms) => agencyOpsApi.deleteTourTerms(sourceId, t.party_tenant_id),
    onSuccess: () => {
      toast.success("Tour terms removed");
      refresh();
    },
    onError: (e) => toast.error(errorText(e)),
  });
  const valid = Number(deposit) >= 0 && Number(deposit) <= 100 && Number(days) >= 0 && Number(days) <= 365 && deposit !== "" && days !== "";
  return (
    <div>
      <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted">Deposit and balance</p>
      {q.isError ? (
        <p className="text-[13px] text-danger">The tour terms could not be loaded.</p>
      ) : !q.data?.items.length ? (
        <p className="text-[13px] text-muted">No tour terms: the agency&apos;s terms or AA&apos;s default apply.</p>
      ) : (
        <ul className="flex flex-col divide-y divide-line2 text-[13px]">
          {q.data.items.map((t) => (
            <li key={t.id} className="flex flex-wrap items-center justify-between gap-2 py-2">
              <span>
                <span className="font-medium text-ink">{t.party_tenant_id ? t.agency_name ?? "One agency" : "Every agency"}</span>
                {" · "}deposit {pctText(t.deposit_pct)}, balance {t.balance_days_before_departure} days before departure
              </span>
              {canWrite && (
                <Button size="sm" variant="ghost" disabled={remove.isPending} onClick={() => remove.mutate(t)} ariaLabel="Remove these terms">
                  <Trash2 size={14} />
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
      {canWrite && (
        <div className="mt-3 flex flex-wrap items-end gap-3">
          <Field label="For">
            <Select
              value={tenant}
              onChange={(e) => setTenant(e.target.value)}
              options={[
                { value: "", label: "Every agency" },
                ...(agencies.data?.items ?? []).map((a) => ({ value: a.tenant_id, label: a.agency_name })),
              ]}
            />
          </Field>
          <Field label="Deposit %">
            <Input inputMode="decimal" value={deposit} onChange={(e) => setDeposit(e.target.value)} style={{ width: 100 }} />
          </Field>
          <Field label="Balance days before">
            <Input type="number" min={0} max={365} value={days} onChange={(e) => setDays(e.target.value)} style={{ width: 120 }} />
          </Field>
          <Button size="sm" variant="primary" disabled={!valid || put.isPending} onClick={() => put.mutate()}>
            Save terms
          </Button>
        </div>
      )}
    </div>
  );
}
