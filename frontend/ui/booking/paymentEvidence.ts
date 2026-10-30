// ui/booking/paymentEvidence.ts
// AA-994 S6d review — which file the payments row menu's "Download evidence" opens.
//
// GET /booking/payments list rows carry `evidence_file_id` but `files: []` (only GET /booking/payments/{ref} reads the
// attached files, backend app/ops/payments.py `payment_out`). So the evidence id wins; the newest attached file is
// the fallback for a detail-shaped row. A removed evidence file clears `evidence_file_id`, so no stale link.

/** The ops-file id to download for a payment row, or null when it has no evidence. */
export function paymentEvidenceFileId(p: {
  evidence_file_id: string | null;
  files?: readonly { id: string }[] | null;
}): string | null {
  return p.evidence_file_id ?? p.files?.at(-1)?.id ?? null;
}
