// app/(staff)/billing/_parts/evidence.ts
// The file and form helpers of the supplier bill and payout drawers (AA-682 S4, BillDrawer.tsx). They were shared with
// the customer payment dialogs of AA-682 S3, retired with the B2C half of Booking (AA-1046).

import { ApiError } from "@/lib/api/client";
import { prepareUpload } from "../../_parts/SupplyContent";

const MAX_FILE_BYTES = 3 * 1024 * 1024;
export const fieldCls = "w-full min-w-0";

export function errorText(e: unknown): string {
  return e instanceof ApiError || e instanceof Error ? e.message : "Something went wrong";
}

function toBase64(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).replace(/^data:[^,]*,/, ""));
    reader.onerror = () => reject(new Error("The file could not be read."));
    reader.readAsDataURL(blob);
  });
}

/** A PDF is sent as it is (≤ 3 MB); a photo of a slip is re-encoded in the browser first (no EXIF reaches S3). */
export async function prepareEvidence(file: File): Promise<{ content_type: string; data_base64: string }> {
  if (file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf")) {
    if (file.size > MAX_FILE_BYTES) throw new Error("A PDF is at most 3 MB here. Save a smaller copy or a photo of it.");
    return { content_type: "application/pdf", data_base64: await toBase64(file) };
  }
  return prepareUpload(file, MAX_FILE_BYTES);
}

export function today(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
