"use client";
// app/(staff)/suppliers/_parts/InventoryTerms.tsx — AA-1153 part 2 (B8): the room and service inventory a contract
// gives AA, under the rates of the Rates tab. An allotment holds N rooms a night (or seats a day) for AA until its
// release days before the date; a free sale lets AA confirm without asking (up to a party size); on request asks the
// supplier every time (also when no term covers a date). B2B quotes answer a room line "available" only from these
// terms of the DMC whose rate priced it. Terms are written while the contract is a draft (the writer becomes its
// maker; a second person publishes them with the contract); release days left empty take the Pricing > Policy default.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
import type { SupplierContract } from "@/lib/api/suppliers";
import { INVENTORY_KINDS, supplyApi, type InventoryKind, type InventoryTerm, type InventoryTermBody } from "@/lib/api/supply";
import { Button, ConfirmDialog, DateField, EmptyState, FormDrawer, Input, Pill, RowMenu, Section, Select, Skeleton, Textarea, useFormat, useToast } from "@/ui";
import { FormField, errorFor, inputCls } from "@/app/(staff)/_parts/FormField";
import { supplyError } from "@/app/(staff)/_parts/supply";
import { textareaCls } from "@/app/(staff)/cases/_parts/fields";
import { ApiError } from "@/lib/api/client";
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { MSG } from "./InventoryTerms.messages";

const KIND_LABEL = { allotment: "Allotment", free_sale: "Free sale", on_request: "On request" } as const satisfies Record<InventoryKind, string>;
const KIND_TONE: Record<InventoryKind, "success" | "info" | "neutral"> = { allotment: "success", free_sale: "info", on_request: "neutral" };

function target(term: InventoryTerm): string {
  return term.room_type_name ? `${term.hotel_code ?? ""} ${term.hotel_name ?? ""} · ${term.room_type_name}` : `${term.service_code ?? ""} ${term.service_title ?? ""}`;
}

/** The release date of an allotment on its first day (the day its unsold units go back to the supplier). */
function releaseOf(term: InventoryTerm): string | null {
  if (term.inventory_kind !== "allotment" || term.release_days == null) return null;
  const d = new Date(`${term.date_from}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() - term.release_days);
  return d.toISOString().slice(0, 10);
}

export function InventoryTerms({ supplierId, contract, canWrite }: { supplierId: string; contract: SupplierContract; canWrite: boolean }) {
  const t = useT(MSG);
  const tx = useTx();
  const { formatDate } = useFormat();
  const qc = useQueryClient();
  const toast = useToast();
  const kinds = useQuery({
    queryKey: ["inventory-terms", contract.id],
    queryFn: async () => {
      const pages = await Promise.all(INVENTORY_KINDS.map((k) => supplyApi.contractTerms(contract.id, k)));
      return pages.flatMap((p) => p.items).sort((a, b) => a.date_from.localeCompare(b.date_from));
    },
  });
  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState<InventoryTerm | null>(null);
  const remove = useMutation({
    mutationFn: (term: InventoryTerm) => supplyApi.deleteTerm(term.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["inventory-terms", contract.id] });
      toast.success(t("Term removed from the draft."));
      setRemoving(null);
    },
  });
  const editable = canWrite && contract.status === "draft";
  const terms = kinds.data ?? [];
  return (
    <Section
      padded={false}
      className="p-4"
      title={t("Room and service inventory")}
      description={t("Allotment, free sale or on request per room type or service. A B2B room line is confirmed at once only from an allotment with rooms left (before its release days) or a free sale of the DMC whose rate priced it; anything else is on request.")}
      actions={
        editable ? (
          <Button variant="secondary" onClick={() => setAdding(true)}>
            <Plus size={15} /> {t("Add term")}
          </Button>
        ) : undefined
      }
    >
      {kinds.isLoading ? (
        <Skeleton h={64} />
      ) : terms.length === 0 ? (
        <EmptyState
          title={t("No inventory terms")}
          description={editable ? t("Every booking is asked of the supplier. Add an allotment or a free sale, or fill the Inventory sheet of the rate sheet.") : t("Every booking of this contract is asked of the supplier (on request).")}
        />
      ) : (
        <ul className="flex flex-col divide-y divide-line text-[13px]" data-testid="inventory-terms">
          {terms.map((term) => {
            const release = releaseOf(term);
            return (
              <li key={term.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2">
                <Pill tone={KIND_TONE[term.inventory_kind]} dot={false}>
                  {t(KIND_LABEL[term.inventory_kind])}
                </Pill>
                <span className="min-w-0 flex-1 break-words text-ink">{tx(target(term))}</span>
                <span className="text-muted">
                  {formatDate(term.date_from)} – {formatDate(term.date_to)}
                </span>
                {term.inventory_kind === "allotment" && (
                  <span className="bk-tabular text-body">
                    {t("{0} a night or day · release {1} days before", term.units ?? 0, term.release_days ?? 0)}
                    {release ? t(" (first night: {0})", formatDate(release)) : ""}
                  </span>
                )}
                {term.inventory_kind === "free_sale" && (
                  <span className="text-body">{term.max_pax ? t("up to {0} travellers a booking", term.max_pax) : t("any party size")}</span>
                )}
                {editable && (
                  <RowMenu
                    label={t("Actions for {0}", target(term))}
                    items={[{ label: t("Remove"), icon: <Trash2 size={15} />, onSelect: () => (remove.reset(), setRemoving(term)), danger: true }]}
                  />
                )}
              </li>
            );
          })}
        </ul>
      )}
      {adding && <TermDrawer supplierId={supplierId} contract={contract} onClose={() => setAdding(false)} />}
      <ConfirmDialog
        open={removing !== null}
        onClose={() => setRemoving(null)}
        onConfirm={() => removing && remove.mutate(removing)}
        busy={remove.isPending}
        error={tx(supplyError(remove.error))}
        danger
        title={t("Remove term")}
        confirmLabel={t("Remove")}
        message={removing ? t("Remove the {0} term of {1} from this draft?", t(KIND_LABEL[removing.inventory_kind]).toLowerCase(), target(removing)) : ""}
      />
    </Section>
  );
}

function TermDrawer({ supplierId, contract, onClose }: { supplierId: string; contract: SupplierContract; onClose: () => void }) {
  const t = useT(MSG);
  const tx = useTx();
  const qc = useQueryClient();
  const toast = useToast();
  const options = useQuery({
    queryKey: ["supply", "options", supplierId],
    queryFn: () => supplyApi.options({ supplier_id: supplierId, limit: 50 }),
  });
  const initial = { target: "", kind: "allotment" as InventoryKind, date_from: contract.valid_from, date_to: contract.valid_to ?? "", units: "", release_days: "", max_pax: "", note: "" };
  const [form, setForm] = useState(initial);
  const [localError, setLocalError] = useState<string | null>(null);
  const set = (k: keyof typeof form) => (v: string) => setForm((f) => ({ ...f, [k]: v }));
  const save = useMutation({
    mutationFn: (body: InventoryTermBody) => supplyApi.createTerm(contract.id, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["inventory-terms", contract.id] });
      toast.success(t("Term added to the draft. Someone else publishes the contract."));
      onClose();
    },
  });
  const whole = (v: string) => (v.trim() === "" ? null : Number(v));

  function submit() {
    if (!form.target) return setLocalError(t("Pick the room type or service."));
    if (!form.date_from || !form.date_to || form.date_to < form.date_from) return setLocalError(t("Pick the first and last day; the last is on or after the first."));
    const units = whole(form.units);
    if (form.kind === "allotment" && !(units && units >= 1)) return setLocalError(t("An allotment needs the rooms (or seats) it holds, 1 or more."));
    setLocalError(null);
    const [prefix, id] = form.target.split(":");
    save.mutate({
      component_id: `${prefix === "room_type" ? "rt" : "sv"}:${id}`,
      inventory_kind: form.kind,
      date_from: form.date_from,
      date_to: form.date_to,
      units: form.kind === "allotment" ? units : null,
      release_days: form.kind === "allotment" ? whole(form.release_days) : null,
      max_pax: form.kind === "free_sale" ? whole(form.max_pax) : null,
      note: form.note.trim() || null,
    });
  }

  const fe = save.error instanceof ApiError ? save.error.fieldErrors : [];
  return (
    <FormDrawer
      open
      onClose={onClose}
      title={t("Add inventory term")}
      submitLabel={t("Add term")}
      onSubmit={submit}
      busy={save.isPending}
      dirty={JSON.stringify(form) !== JSON.stringify(initial)}
      error={localError ?? (fe.length ? null : supplyError(save.error))}
      fieldErrors={fe}
      width={520}
    >
      <p className="text-[13px] text-muted">
        {t("Under")} <strong className="text-ink">{tx(contract.title)}</strong>
        {t(". Saving makes you this draft's maker: another person publishes it with its terms.")}
      </p>
      <FormField label={t("For")} required hint={t("Room types of hotels this supplier sells, and its services.")} error={errorFor(fe, "component_id")}>
        <Select className={inputCls} value={form.target} onChange={(e) => set("target")(e.target.value)} disabled={options.isLoading}>
          <option value="">{options.isLoading ? t("Loading…") : t("Pick a room type or service")}</option>
          {(options.data?.items ?? []).map((o) => (
            <option key={`${o.kind}:${o.id}`} value={`${o.kind}:${o.id}`}>
              {o.kind === "room_type" ? t("Room · ") : t("Service · ")}
              {tx(o.label)} ({o.code})
            </option>
          ))}
        </Select>
      </FormField>
      <FormField label={t("Kind")} required error={errorFor(fe, "inventory_kind")}>
        <Select className={inputCls} value={form.kind} onChange={(e) => set("kind")(e.target.value)}>
          {INVENTORY_KINDS.map((k) => (
            <option key={k} value={k}>
              {t(KIND_LABEL[k])}
            </option>
          ))}
        </Select>
      </FormField>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <FormField label={t("From")} required error={errorFor(fe, "date_from")}>
          <DateField className={inputCls} value={form.date_from} min={contract.valid_from} max={contract.valid_to ?? undefined} onChange={(v) => set("date_from")(v)} />
        </FormField>
        <FormField label={t("To")} required hint={t("Inside the contract.")} error={errorFor(fe, "date_to")}>
          <DateField className={inputCls} value={form.date_to} min={form.date_from} max={contract.valid_to ?? undefined} onChange={(v) => set("date_to")(v)} />
        </FormField>
        {form.kind === "allotment" && (
          <>
            <FormField label={t("Rooms or seats")} required hint={t("A night (rooms) or a day (seats).")} error={errorFor(fe, "units")}>
              <Input type="number" min={1} max={999} className={inputCls} value={form.units} onChange={(e) => set("units")(e.target.value)} />
            </FormField>
            <FormField label={t("Release days")} hint={t("Empty = the Pricing policy default.")} error={errorFor(fe, "release_days")}>
              <Input type="number" min={0} max={365} className={inputCls} value={form.release_days} onChange={(e) => set("release_days")(e.target.value)} />
            </FormField>
          </>
        )}
        {form.kind === "free_sale" && (
          <FormField label={t("Most travellers a booking")} hint={t("Empty = any party size.")} error={errorFor(fe, "max_pax")}>
            <Input type="number" min={1} max={999} className={inputCls} value={form.max_pax} onChange={(e) => set("max_pax")(e.target.value)} />
          </FormField>
        )}
      </div>
      <FormField label={t("Note")} hint={t("Staff only.")} error={errorFor(fe, "note")}>
        <Textarea className={textareaCls} maxLength={300} value={form.note} onChange={(e) => set("note")(e.target.value)} />
      </FormField>
    </FormDrawer>
  );
}
