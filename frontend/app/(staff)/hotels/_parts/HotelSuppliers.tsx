"use client";
// app/(staff)/hotels/_parts/HotelSuppliers.tsx — AA-1153 (bk-069, T8 / T10 / T11): the property master part of a hotel.
// AA has no contract with a hotel: DMCs (AA's suppliers) sell it to AA, each under its own supplier contract. This
// block lists the DMCs that sell the hotel (their own code for it, the preferred one whose price a quote takes first),
// the names DMCs use for its room types (the rate-sheet import matches them), the codes of other systems and the last
// confirmation of the facts (due again after the Pricing > Policy interval). Writes need supply.write.

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Plus, Star, X } from "lucide-react";
import { supplyApi, type FactsSource, type HotelDetail, type HotelSupplierLink } from "@/lib/api/supply";
import { Button, DateText, Input, Pill, Select, useToast } from "@/ui";
import { SupplierSelect, supplyError } from "@/app/(staff)/_parts/supply";
import { inputCls } from "@/app/(staff)/_parts/FormField";
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { MSG } from "./HotelSuppliers.messages";

const SOURCES: FactsSource[] = ["dmc_sheet", "hotel_website", "site_visit"];

/** A write on the hotel's property master: refresh the hotel on success, toast the outcome. */
function useHotelMutation<A>(fn: (a: A) => Promise<unknown>, done: string) {
  const qc = useQueryClient();
  const toast = useToast();
  const t = useT(MSG);
  return useMutation({
    mutationFn: fn,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["hotel"] });
      qc.invalidateQueries({ queryKey: ["hotels"] });
      toast.success(done);
    },
    onError: (err) => toast.error(supplyError(err) ?? t("Could not save.")),
  });
}

export function HotelSuppliers({ h, canWrite }: { h: HotelDetail; canWrite: boolean }) {
  const t = useT(MSG);
  const links = h.suppliers ?? [];
  return (
    <section className="flex min-w-0 flex-col gap-3" data-testid="hotel-suppliers">
      <h3 className="text-[13px] font-semibold uppercase tracking-[0.04em] text-muted">{t("Suppliers that sell this hotel")}</h3>
      <p className="text-xs text-muted">
        {t("AA books the hotel through these suppliers (DMCs), each under its own contract. A quote takes the preferred supplier's price first, then the lowest one for the same board; the booking goes to the supplier of the price used.")}
      </p>
      <ul className="divide-y divide-line2 rounded-lg border border-line bg-card">
        {links.map((l) => (
          <LinkRow key={l.supplier_id} h={h} link={l} canWrite={canWrite} />
        ))}
      </ul>
      {canWrite && <AddLink h={h} />}
      <RoomNames h={h} canWrite={canWrite} />
      <FactsConfirmation h={h} canWrite={canWrite} />
    </section>
  );
}

function LinkRow({ h, link, canWrite }: { h: HotelDetail; link: HotelSupplierLink; canWrite: boolean }) {
  const t = useT(MSG);
  const change = useHotelMutation(
    (body: { is_preferred?: boolean; status?: "active" | "inactive" }) => supplyApi.updateSupplierLink(h.code, link.supplier_id, body),
    t("Supplier link saved."),
  );
  return (
    <li className="flex flex-wrap items-center justify-between gap-2 px-4 py-2.5 text-[13px]">
      <span className="flex min-w-0 flex-wrap items-center gap-2">
        <span className="font-medium text-ink">{link.supplier_name}</span>
        <span className="text-xs text-muted">{link.supplier_code}</span>
        {link.supplier_hotel_code && <span className="text-xs text-muted">{t("their code {0}", link.supplier_hotel_code)}</span>}
        {link.is_preferred && <Pill tone="accent">{t("Preferred")}</Pill>}
        {link.status === "inactive" && <Pill tone="neutral">{t("Inactive")}</Pill>}
        {link.first_listed && <span className="text-xs text-muted">{t("listed the hotel first")}</span>}
      </span>
      {canWrite && (
        <span className="flex gap-1">
          {link.status === "active" && !link.is_preferred && (
            <Button size="sm" variant="ghost" disabled={change.isPending} onClick={() => change.mutate({ is_preferred: true })}>
              <Star size={13} /> {t("Make preferred")}
            </Button>
          )}
          {link.is_preferred && (
            <Button size="sm" variant="ghost" disabled={change.isPending} onClick={() => change.mutate({ is_preferred: false })}>
              {t("No preference")}
            </Button>
          )}
          <Button
            size="sm"
            variant="ghost"
            disabled={change.isPending}
            onClick={() => change.mutate({ status: link.status === "active" ? "inactive" : "active" })}
          >
            {link.status === "active" ? t("Stop") : t("Resume")}
          </Button>
        </span>
      )}
    </li>
  );
}

function AddLink({ h }: { h: HotelDetail }) {
  const t = useT(MSG);
  const [supplier, setSupplier] = useState("");
  const [code, setCode] = useState("");
  const add = useHotelMutation(
    () => supplyApi.linkSupplier(h.code, { supplier_id: supplier, supplier_hotel_code: code.trim() || null }),
    t("Supplier linked to the hotel."),
  );
  return (
    <div className="grid grid-cols-1 items-end gap-2 sm:grid-cols-[1fr_180px_auto]">
      <label className="flex min-w-0 flex-col gap-1 text-xs font-semibold text-muted">
        {t("Another supplier that sells it")}
        <SupplierSelect value={supplier} category="hotel" onChange={setSupplier} />
      </label>
      <label className="flex min-w-0 flex-col gap-1 text-xs font-semibold text-muted">
        {t("Their code for the hotel")}
        <Input className={inputCls} maxLength={64} value={code} onChange={(e) => setCode(e.target.value)} />
      </label>
      <Button variant="secondary" disabled={!supplier || add.isPending} onClick={() => add.mutate(undefined)}>
        <Plus size={13} /> {t("Link supplier")}
      </Button>
    </div>
  );
}

function RoomNames({ h, canWrite }: { h: HotelDetail; canWrite: boolean }) {
  const t = useT(MSG);
  const tx = useTx();
  const [room, setRoom] = useState("");
  const [supplier, setSupplier] = useState("");
  const [alias, setAlias] = useState("");
  const add = useHotelMutation(
    () => supplyApi.addAlias(room, { alias: alias.trim(), supplier_id: supplier || null }),
    t("Room type name added."),
  );
  const remove = useHotelMutation((id: string) => supplyApi.removeAlias(id), t("Room type name removed."));
  const rooms = h.room_types.filter((r) => r.status === "active");
  const named = h.room_types.filter((r) => (r.aliases ?? []).length);
  return (
    <div className="flex flex-col gap-2">
      <h4 className="text-xs font-semibold text-muted">{t("Room type names suppliers use")}</h4>
      {named.length === 0 ? (
        <p className="text-xs text-muted">{t("None yet: a rate sheet matches the AA room type name only.")}</p>
      ) : (
        <ul className="flex flex-col gap-1 text-[13px]">
          {named.map((r) => (
            <li key={r.id} className="flex flex-wrap items-center gap-1.5">
              <span className="font-medium text-ink">{r.name}:</span>
              {(r.aliases ?? []).map((a) => (
                <span key={a.id} className="inline-flex items-center gap-1 rounded-full border border-line bg-panel px-2 py-0.5 text-[11.5px]">
                  {tx(a.alias)}
                  {a.supplier_code && <span className="text-muted">({a.supplier_code})</span>}
                  {canWrite && (
                    <Button size="sm" variant="ghost" ariaLabel={t("Remove {0}", a.alias)} onClick={() => remove.mutate(a.id)}>
                      <X size={11} />
                    </Button>
                  )}
                </span>
              ))}
            </li>
          ))}
        </ul>
      )}
      {canWrite && rooms.length > 0 && (
        <div className="grid grid-cols-1 items-end gap-2 sm:grid-cols-[1fr_1fr_1fr_auto]">
          <Select className={inputCls} value={room} onChange={(e) => setRoom(e.target.value)} aria-label={t("Room type")}>
            <option value="">{t("Room type")}</option>
            {rooms.map((r) => (
              <option key={r.id} value={r.id}>
                {r.name}
              </option>
            ))}
          </Select>
          <Select className={inputCls} value={supplier} onChange={(e) => setSupplier(e.target.value)} aria-label={t("Supplier")}>
            <option value="">{t("Any supplier")}</option>
            {(h.suppliers ?? []).map((l) => (
              <option key={l.supplier_id} value={l.supplier_id}>
                {l.supplier_name}
              </option>
            ))}
          </Select>
          <Input className={inputCls} maxLength={120} value={alias} placeholder={t("Their name for it")} onChange={(e) => setAlias(e.target.value)} />
          <Button variant="secondary" disabled={!room || !alias.trim() || add.isPending} onClick={() => add.mutate(undefined)}>
            <Plus size={13} /> {t("Add name")}
          </Button>
        </div>
      )}
    </div>
  );
}

function FactsConfirmation({ h, canWrite }: { h: HotelDetail; canWrite: boolean }) {
  const t = useT(MSG);
  const [source, setSource] = useState<FactsSource>("dmc_sheet");
  const [supplier, setSupplier] = useState(h.suppliers?.[0]?.supplier_id ?? "");
  const confirm = useHotelMutation(
    () => supplyApi.confirmFacts(h.code, { source, supplier_id: source === "dmc_sheet" ? supplier || null : null }),
    t("Facts confirmed."),
  );
  const label: Record<FactsSource, string> = {
    dmc_sheet: t("Supplier's sheet"),
    hotel_website: t("Hotel website"),
    site_visit: t("Site visit"),
  };
  const by = h.suppliers?.find((l) => l.supplier_id === h.facts_source_supplier_id)?.supplier_name;
  const overdue = !h.facts_reconfirm_due || h.facts_reconfirm_due <= new Date().toISOString().slice(0, 10);
  const codes = Object.entries(h.external_codes ?? {});
  return (
    <div className="flex flex-col gap-2 rounded-lg border border-line bg-card p-3 text-[13px]">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium text-ink">{t("Facts confirmed")}</span>
        {h.facts_confirmed_on ? (
          <span className="text-body">
            <DateText value={h.facts_confirmed_on} /> · {label[h.facts_source ?? "dmc_sheet"]}
            {by ? ` (${by})` : ""}
          </span>
        ) : (
          <span className="text-muted">{t("Never")}</span>
        )}
        <Pill tone={overdue ? "warning" : "success"}>
          {h.facts_reconfirm_due ? t("Due again {0}", h.facts_reconfirm_due) : t("Confirmation due")}
        </Pill>
      </div>
      {codes.length > 0 && (
        <p className="text-xs text-muted">
          {t("Codes in other systems:")} {codes.map(([k, v]) => `${k} ${v}`).join(" · ")}
        </p>
      )}
      {canWrite && (
        <div className="grid grid-cols-1 items-end gap-2 sm:grid-cols-[1fr_1fr_auto]">
          <Select className={inputCls} value={source} onChange={(e) => setSource(e.target.value as FactsSource)} aria-label={t("Source")}>
            {SOURCES.map((s) => (
              <option key={s} value={s}>
                {label[s]}
              </option>
            ))}
          </Select>
          <Select
            className={inputCls}
            value={supplier}
            disabled={source !== "dmc_sheet"}
            onChange={(e) => setSupplier(e.target.value)}
            aria-label={t("Supplier")}
          >
            {(h.suppliers ?? []).map((l) => (
              <option key={l.supplier_id} value={l.supplier_id}>
                {l.supplier_name}
              </option>
            ))}
          </Select>
          <Button variant="secondary" disabled={confirm.isPending || (source === "dmc_sheet" && !supplier)} onClick={() => confirm.mutate(undefined)}>
            <CheckCircle2 size={13} /> {t("Confirm the facts today")}
          </Button>
        </div>
      )}
    </div>
  );
}
