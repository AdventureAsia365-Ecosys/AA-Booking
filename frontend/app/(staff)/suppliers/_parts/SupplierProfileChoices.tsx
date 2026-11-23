"use client";
// app/(staff)/suppliers/_parts/SupplierProfileChoices.tsx — AA-1158 part 2 (review S289): the choices of the supplier
// profile (contact roles, payment terms bases and methods) are data an administrator edits (staff.admin, like the role
// editor), not code lists. GET / POST / PATCH /booking/supplier-profile-options: add a choice (its code never changes),
// edit its English and Vietnamese labels and its order, retire it (suppliers that hold it keep it) or turn it on again.
// Every change is in the audit log. Opened from the supplier list ("Profile choices").

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import {
  profileOptionsApi,
  type NewProfileOption,
  type ProfileOption,
  type ProfileOptionChange,
  type ProfileOptionKind,
} from "@/lib/api/suppliers";
import { Button, Drawer, Input, Section, Switch, useToast } from "@/ui";
import { inputCls } from "@/app/(staff)/_parts/FormField";
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { PROFILE_OPTIONS_KEY } from "./SupplierLegalTerms";
import { supplierError } from "./shared";
import { MSG } from "./SupplierProfileChoices.messages";

const KINDS: { kind: ProfileOptionKind; title: "Contact roles" | "Payment bases" | "Payment methods" }[] = [
  { kind: "contact_role", title: "Contact roles" },
  { kind: "terms_basis", title: "Payment bases" },
  { kind: "terms_method", title: "Payment methods" },
];

export function SupplierProfileChoices({ open, onClose }: { open: boolean; onClose: () => void }) {
  const t = useT(MSG);
  const q = useQuery({ queryKey: PROFILE_OPTIONS_KEY, queryFn: profileOptionsApi.list, enabled: open });
  return (
    <Drawer open={open} onClose={onClose} title={t("Supplier profile choices")} width={640}>
      <div className="flex flex-col gap-5">
        <p className="text-[13px] text-muted">
          {t("The choices offered on every supplier's profile. A code never changes; a retired choice stays on the suppliers that hold it and is not offered again.")}
        </p>
        {q.isLoading && <p className="text-[13px] text-muted">{t("Loading the choices")}</p>}
        {KINDS.map(({ kind, title }) => (
          <Section key={kind} title={t(title)}>
            <ul className="flex flex-col gap-2">
              {(q.data?.kinds[kind] ?? []).map((o) => (
                <ChoiceRow key={o.code} option={o} />
              ))}
            </ul>
            <AddChoice kind={kind} />
          </Section>
        ))}
      </div>
    </Drawer>
  );
}

function useChoiceWrite<V>(fn: (v: V) => Promise<ProfileOption>, done: string, after?: () => void) {
  const qc = useQueryClient();
  const toast = useToast();
  return useMutation({
    mutationFn: fn,
    onSuccess: () => {
      toast.success(done);
      qc.invalidateQueries({ queryKey: PROFILE_OPTIONS_KEY });
      after?.();
    },
  });
}

function ChoiceRow({ option: o }: { option: ProfileOption }) {
  const t = useT(MSG);
  const tx = useTx();
  const [en, setEn] = useState(o.label_en);
  const [vi, setVi] = useState(o.label_vi);
  const [pos, setPos] = useState(String(o.position));
  const save = useChoiceWrite((body: ProfileOptionChange) => profileOptionsApi.change(o.kind, o.code, body), t("Choice saved"));
  const changed = en !== o.label_en || vi !== o.label_vi || pos !== String(o.position);
  return (
    <li className="flex flex-col gap-2 rounded-lg border border-line bg-card px-3 py-2 text-[13px]">
      <div className="grid gap-2 sm:grid-cols-[110px_minmax(0,1fr)_minmax(0,1fr)_70px]">
        <code className="self-center truncate text-muted">{o.code}</code>
        <Input className={inputCls} aria-label={t("English label")} maxLength={80} value={en} onChange={(e) => setEn(e.target.value)} />
        <Input className={inputCls} aria-label={t("Vietnamese label")} maxLength={80} value={vi} onChange={(e) => setVi(e.target.value)} />
        <Input className={inputCls} aria-label={t("Order")} type="number" min={1} max={999} value={pos} onChange={(e) => setPos(e.target.value)} />
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <Switch label={t("Offered")} checked={o.active} disabled={save.isPending} onChange={(active) => save.mutate({ active })} />
        <span className="text-muted">{o.active ? t("Offered") : t("Retired")}</span>
        {changed && (
          <Button size="sm" variant="primary" disabled={save.isPending} onClick={() => save.mutate({ label_en: en.trim(), label_vi: vi.trim(), position: Number(pos) })}>
            {t("Save")}
          </Button>
        )}
        {save.isError && <span role="alert" className="text-danger">{tx(supplierError(save.error))}</span>}
      </div>
    </li>
  );
}

const BLANK = { code: "", label_en: "", label_vi: "" };

function AddChoice({ kind }: { kind: ProfileOptionKind }) {
  const t = useT(MSG);
  const tx = useTx();
  const [form, setForm] = useState(BLANK);
  const add = useChoiceWrite((body: NewProfileOption) => profileOptionsApi.add(body), t("Choice added"), () => setForm(BLANK));
  const set = (k: keyof typeof BLANK) => (e: React.ChangeEvent<HTMLInputElement>) => setForm((cur) => ({ ...cur, [k]: e.target.value }));
  const ready = form.code.trim() && form.label_en.trim() && form.label_vi.trim();
  return (
    <div className="mt-3 flex flex-col gap-2">
      <div className="grid gap-2 sm:grid-cols-[110px_minmax(0,1fr)_minmax(0,1fr)_auto]">
        <Input className={inputCls} aria-label={t("New code")} placeholder={t("code")} maxLength={40} value={form.code} onChange={set("code")} />
        <Input className={inputCls} aria-label={t("English label")} placeholder={t("English label")} maxLength={80} value={form.label_en} onChange={set("label_en")} />
        <Input className={inputCls} aria-label={t("Vietnamese label")} placeholder={t("Vietnamese label")} maxLength={80} value={form.label_vi} onChange={set("label_vi")} />
        <Button size="sm" variant="secondary" disabled={!ready || add.isPending} onClick={() => add.mutate({ kind, code: form.code.trim(), label_en: form.label_en.trim(), label_vi: form.label_vi.trim() })}>
          <Plus size={14} /> {t("Add")}
        </Button>
      </div>
      {add.isError && <p role="alert" className="text-[13px] text-danger">{tx(supplierError(add.error))}</p>}
    </div>
  );
}
