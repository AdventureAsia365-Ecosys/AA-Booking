"use client";
// app/(staff)/billing/_parts/BillFields.tsx — the labelled form field and the read-only fact of the supplier bill drawer
// (moved out of BillDrawer.tsx for AA-1128 B5, which kept that file under its line budget). Labels are English
// catalogue text shown in the UI language through the phrasebook (useTx).

import { useTx } from "@/ui/local/useTx";

export function Field({ label, required = false, children, hint }: { label: string; required?: boolean; children: React.ReactNode; hint?: string }) {
  const tx = useTx();
  return (
    <label className="flex min-w-0 flex-col gap-1">
      <span className="text-xs font-semibold text-muted">
        {tx(label)} {required && <span className="text-danger">*</span>}
      </span>
      {children}
      {hint && <span className="text-xs text-muted">{tx(hint)}</span>}
    </label>
  );
}

export function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  const tx = useTx();
  return (
    <div className="flex min-w-0 flex-col">
      <dt className="text-xs text-muted">{tx(label)}</dt>
      <dd className="truncate text-[13px] text-ink">{children}</dd>
    </div>
  );
}
