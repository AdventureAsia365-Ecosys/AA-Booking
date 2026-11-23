"use client";
// app/(staff)/billing/_parts/OwnerPathNotice.tsx — AA-1158: the bill drawer says when the caller's approval is a
// business-owner self-approval, or her payout is of a bill she approved (both flagged and listed in the audit log).

import type { SupplierBill } from "@/lib/api/supplierBills";
import { useT } from "@/ui/core/useI18n";
import { MSG } from "./BillDrawer.messages";

export function OwnerPathNotice({ actions }: { actions: SupplierBill["actions"] }) {
  const t = useT(MSG);
  if (actions.approve_self) {
    return <p className="text-xs text-muted">{t("You entered or changed this bill: approving it is recorded as a business-owner self-approval.")}</p>;
  }
  if (actions.pay_flagged) return <p className="text-xs text-muted">{t("You approved this bill: this payout is flagged.")}</p>;
  return null;
}
