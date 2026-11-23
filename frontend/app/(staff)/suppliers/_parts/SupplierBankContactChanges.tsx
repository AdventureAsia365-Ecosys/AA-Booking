"use client";
// app/(staff)/suppliers/_parts/SupplierBankContactChanges.tsx — AA-1158 part 2 (review S289): while a bank account
// change waits, the approver sees every change of the supplier's contacts in the policy window (when, who, which
// fields; never the values) before calling back. A change by the person who entered the account, or by the approver,
// closes the approval (the API refuses it, 409 contacts_recently_changed).

import { UserRoundPen } from "lucide-react";
import type { ContactChange, SupplierBankList } from "@/lib/api/supplierBank";
import { DateText } from "@/ui";
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { MSG } from "./SupplierBankContactChanges.messages";

const FIELD_LABEL: Record<ContactChange["fields"][number], string> = {
  contact_name: "contact name",
  contact_email: "contact e-mail",
  contact_phone: "contact phone",
  role_contacts: "contacts by role",
};

export function ContactChanges({ changes }: { changes: SupplierBankList["contact_changes"] }) {
  const t = useT(MSG);
  const tx = useTx();
  if (!changes || changes.items.length === 0) return null;
  return (
    <div role="status" className="mb-3 flex flex-col gap-1 rounded-lg border border-warning-border bg-warning-soft px-4 py-3 text-[13px] text-body">
      <span className="flex items-center gap-2 font-medium text-ink">
        <UserRoundPen size={15} /> {t("The supplier's contacts changed in the last {0} days. Call back on a number from the contract or earlier records.", changes.days ?? "")}
      </span>
      <ul className="flex flex-col gap-0.5">
        {changes.items.map((c, i) => (
          <li key={i}>
            <DateText value={c.at} withTime /> · {c.by?.name ?? "—"} · {c.fields.map((f) => tx(FIELD_LABEL[f] ?? f)).join(", ")}
          </li>
        ))}
      </ul>
    </div>
  );
}
