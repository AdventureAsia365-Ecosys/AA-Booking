"use client";
// app/(staff)/billing/_parts/OwnerPathNotice.tsx — AA-1158: the bill drawer says when the caller's approval is a
// business-owner self-approval, or her payout is of a bill she approved (both flagged and listed in the audit log).
// Part 2 (bk-067): it also says when payouts to the supplier are paused after a bank account change.

import type { PayoutLock } from "@/lib/api/supplierBank";
import type { SupplierBill } from "@/lib/api/supplierBills";
import { DateText } from "@/ui";
import { useT } from "@/ui/core/useI18n";
import { MSG } from "./BillDrawer.messages";

export function OwnerPathNotice({ actions, lock }: { actions: SupplierBill["actions"]; lock?: PayoutLock }) {
  const t = useT(MSG);
  if (lock?.state === "pending" || lock?.state === "unknown") {
    return <p className="text-xs text-warning">{t("Payouts to this supplier are paused: its bank account change waits for approval.")}</p>;
  }
  if (lock?.state === "locked") {
    return (
      <p className="text-xs text-warning">
        {t("Payouts to this supplier are paused after a bank account change; they open at")} <DateText value={lock.until ?? ""} withTime />
      </p>
    );
  }
  if (actions.approve_self) {
    return <p className="text-xs text-muted">{t("You entered or changed this bill: approving it is recorded as a business-owner self-approval.")}</p>;
  }
  if (actions.pay_flagged) return <p className="text-xs text-muted">{t("You approved this bill: this payout is flagged.")}</p>;
  return null;
}
