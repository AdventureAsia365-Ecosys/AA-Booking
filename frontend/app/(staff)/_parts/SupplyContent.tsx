"use client";
// app/(staff)/_parts/SupplyContent.tsx — AA-835 S4 (root ADR 0004a), shared by the hotel drawer and the service page:
//
// - CisContentPanel: the customer content CIS published for the record, read-only, with its status and an "Edit in
//   CIS" link. The link is CIS_ADMIN_URL + the `edit_url` path CIS serves (never built from Booking ids). Hidden while
//   the API says `content: null` (CIS 278 not served yet): the read-back turns itself on when CIS ships the view.
// - SupplierPhotos: photos contracting received from the supplier. Upload (JPEG / PNG / WebP; every photo is
//   re-encoded in the browser first: that strips EXIF data such as GPS, gives a type that matches the bytes even
//   when the file name lies, and downsizes it, since the BFF and API take at most 3 MB of image), a "usage rights"
//   switch (CIS publishes a photo only while it is on), and Remove (final; the file is kept). Writes need
//   supply.write, so a Preview-host session (read-only) and sales only see the photos.

import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, ImagePlus, ShieldCheck, Trash2 } from "lucide-react";
import { CIS_ADMIN_URL } from "@/lib/config";
import { MEDIA_TYPES, supplyApi, type MediaType, type SupplyContent, type SupplyMedia } from "@/lib/api/supply";
import { Button, ConfirmDialog, DateText, FromCisTag, Pill, Skeleton, useToast, type BookingTone } from "@/ui";
import { supplyError } from "./supply";

type Kind = "hotel" | "service";

const STATUS_META: Record<SupplyContent["content_status"], { label: string; tone: BookingTone; hint: string }> = {
  none: { label: "Not drafted", tone: "neutral", hint: "CIS has not drafted customer content for this record yet." },
  draft: { label: "Draft", tone: "warning", hint: "CIS drafted content; it waits for review. Proposals show none yet." },
  in_review: { label: "In review", tone: "info", hint: "A CIS reviewer is checking the draft. Proposals show none yet." },
  published: { label: "Published", tone: "success", hint: "Proposals show this description." },
  stale: {
    label: "Needs update",
    tone: "orange",
    hint: "The facts changed after publishing; CIS is redrafting. Proposals show no description until it is republished.",
  },
  unpublished: { label: "Unpublished", tone: "neutral", hint: "The record is inactive, so CIS took its content down." },
};

const EDIT_PATH = /^\/admin\/supply-content\/[A-Za-z0-9_-]{1,64}$/;

export function cisEditUrl(path: string | null | undefined): string | null {
  return path && EDIT_PATH.test(path) ? `${CIS_ADMIN_URL}${path}` : null;
}

export function CisContentPanel({ content }: { content: SupplyContent | null | undefined }) {
  if (!content) return null;
  const meta = STATUS_META[content.content_status] ?? STATUS_META.none;
  const edit = cisEditUrl(content.edit_url);
  return (
    <section className="min-w-0" aria-labelledby="cis-content-title">
      <header className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h3 id="cis-content-title" className="flex items-center gap-2 text-[13px] font-semibold uppercase tracking-[0.04em] text-muted">
          Customer content <FromCisTag />
        </h3>
        {edit && (
          <a href={edit} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-[12px] font-medium text-accent-deep hover:underline">
            Edit in CIS <ExternalLink size={12} />
          </a>
        )}
      </header>
      <div className="flex flex-col gap-3 rounded-lg border border-line bg-card p-4">
        <p className="flex flex-wrap items-center gap-2 text-[13px] text-muted">
          <Pill tone={meta.tone}>{meta.label}</Pill>
          {content.content_version != null && <span>Version {content.content_version}</span>}
          <span className="basis-full sm:basis-auto">{meta.hint}</span>
        </p>
        {content.description && <p className="whitespace-pre-line text-[13.5px] leading-relaxed text-body">{content.description}</p>}
        {content.highlights.length > 0 && (
          <ul className="list-disc pl-5 text-[13px] text-body">
            {content.highlights.map((h) => (
              <li key={h}>{h}</li>
            ))}
          </ul>
        )}
        {content.photo_urls.length > 0 && (
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            {content.photo_urls.map((url, i) => (
              // eslint-disable-next-line @next/next/no-img-element -- CIS CDN image, already sized (see ui/booking/CisPhoto.tsx)
              <img
                key={url}
                src={url}
                alt={`Published photo ${i + 1}`}
                loading="lazy"
                decoding="async"
                className="aspect-[4/3] w-full rounded-md bg-panel object-cover"
              />
            ))}
          </div>
        )}
      </div>
    </section>
  );
}

// ── Photos ───────────────────────────────────────────────────────────────────

const DEFAULT_MAX_BYTES = 3 * 1024 * 1024;
const MAX_EDGE = 2560;

function isMediaType(t: string): t is MediaType {
  return (MEDIA_TYPES as readonly string[]).includes(t);
}

function toBase64(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).replace(/^data:[^,]*,/, ""));
    reader.onerror = () => reject(new Error("The file could not be read."));
    reader.readAsDataURL(blob);
  });
}

function encode(canvas: HTMLCanvasElement, type: string, quality: number): Promise<Blob | null> {
  return new Promise((resolve) => canvas.toBlob(resolve, type, quality));
}

/** The bytes to send: always a re-encoded WebP (JPEG where the browser cannot encode WebP), downsized until it fits.
 *  Never the original file: re-encoding drops its metadata (EXIF, including GPS, never reaches S3), and `content_type`
 *  is the encoded blob's type, so a mislabelled file (a PNG saved as .jpg) cannot fail with content_type_mismatch. */
export async function prepareUpload(file: File, maxBytes: number): Promise<{ content_type: MediaType; data_base64: string }> {
  let bitmap: ImageBitmap;
  try {
    bitmap = await createImageBitmap(file);
  } catch {
    throw new Error("This file is not a photo the browser can read. Use JPEG, PNG or WebP.");
  }
  let edge = Math.min(MAX_EDGE, Math.max(bitmap.width, bitmap.height));
  for (let attempt = 0; attempt < 6; attempt += 1) {
    const scale = edge / Math.max(bitmap.width, bitmap.height);
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(bitmap.width * scale));
    canvas.height = Math.max(1, Math.round(bitmap.height * scale));
    canvas.getContext("2d")?.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    const quality = attempt < 3 ? 0.85 - attempt * 0.1 : 0.7;
    let blob = await encode(canvas, "image/webp", quality);
    if (!blob || blob.type !== "image/webp") blob = await encode(canvas, "image/jpeg", quality);
    if (blob && isMediaType(blob.type) && blob.size <= maxBytes) {
      bitmap.close();
      return { content_type: blob.type, data_base64: await toBase64(blob) };
    }
    if (attempt >= 2) edge = Math.round(edge * 0.75);
  }
  bitmap.close();
  throw new Error("The photo is too large even after resizing. Use a smaller photo.");
}

function bytesLabel(n: number): string {
  return n >= 1024 * 1024 ? `${(n / (1024 * 1024)).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`;
}

export function SupplierPhotos({
  kind,
  recordRef,
  recordActive,
  canWrite,
}: {
  kind: Kind;
  recordRef: string;
  recordActive: boolean;
  canWrite: boolean;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const input = useRef<HTMLInputElement>(null);
  const [preparing, setPreparing] = useState(false);
  const [removing, setRemoving] = useState<SupplyMedia | null>(null);
  const q = useQuery({ queryKey: ["media", kind, recordRef], queryFn: () => supplyApi.media(kind, recordRef) });
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["media", kind, recordRef] });
    qc.invalidateQueries({ queryKey: [kind] });
  };

  const upload = useMutation({
    mutationFn: async (file: File) => {
      setPreparing(true);
      try {
        const prepared = await prepareUpload(file, q.data?.max_bytes ?? DEFAULT_MAX_BYTES);
        return await supplyApi.uploadMedia(kind, recordRef, { ...prepared, filename: file.name.slice(0, 200) });
      } finally {
        setPreparing(false);
      }
    },
    onSuccess: () => {
      refresh();
      toast.success("Photo added. Confirm its usage rights before CIS can publish it.");
    },
    onError: (err) => toast.error(mediaError(err) ?? "Could not add the photo."),
  });
  const update = useMutation({
    mutationFn: ({ id, body }: { id: string; body: { usage_rights?: boolean; status?: "removed" } }) => supplyApi.updateMedia(id, body),
    onSuccess: (m) => {
      refresh();
      setRemoving(null);
      toast.success(m.status === "removed" ? "Photo removed." : m.usage_rights ? "Usage rights confirmed." : "Usage rights withdrawn.");
    },
    onError: (err) => toast.error(mediaError(err) ?? "Could not change the photo."),
  });

  const items = q.data?.items ?? [];
  const full = q.data ? items.length >= q.data.max_active : false;
  const busy = upload.isPending || preparing;
  return (
    <section className="min-w-0" aria-labelledby={`${kind}-photos-title`}>
      <header className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h3 id={`${kind}-photos-title`} className="text-[13px] font-semibold uppercase tracking-[0.04em] text-muted">
          Supplier photos ({items.length})
        </h3>
        {canWrite && recordActive && (
          <>
            <input
              ref={input}
              type="file"
              accept="image/jpeg,image/png,image/webp"
              className="sr-only"
              aria-label="Choose a photo to upload"
              onChange={(e) => {
                const file = e.target.files?.[0];
                e.target.value = "";
                if (file) upload.mutate(file);
              }}
            />
            <Button size="sm" variant="primary" disabled={busy || full} onClick={() => input.current?.click()} title={full ? "Remove a photo first" : undefined}>
              <ImagePlus size={13} /> {busy ? "Uploading…" : "Add photo"}
            </Button>
          </>
        )}
      </header>
      <p className="mb-2 text-xs text-muted">
        CIS uses a photo for customer content only after its usage rights are confirmed (the supplier allows AA to publish it).
      </p>
      {q.isLoading ? (
        <Skeleton h={96} />
      ) : q.isError ? (
        <p role="alert" className="text-[13px] text-danger">
          {supplyError(q.error, "Could not load the photos.")}
        </p>
      ) : items.length === 0 ? (
        <p className="rounded-lg border border-dashed border-line px-4 py-5 text-center text-[13px] text-muted">
          No photos yet.{canWrite && recordActive ? " Add the photos the supplier sent (JPEG, PNG or WebP)." : ""}
        </p>
      ) : (
        <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {items.map((m) => (
            <li key={m.id} className="flex min-w-0 flex-col overflow-hidden rounded-lg border border-line bg-card">
              {m.url ? (
                // eslint-disable-next-line @next/next/no-img-element -- presigned private S3 URL; next/image would need the bucket host
                <img src={m.url} alt={`Supplier photo, ${m.width ?? "?"} × ${m.height ?? "?"}`} loading="lazy" className="aspect-[4/3] w-full bg-panel object-cover" />
              ) : (
                <div className="grid aspect-[4/3] w-full place-items-center bg-panel text-xs text-muted">Preview not available</div>
              )}
              <div className="flex flex-col gap-2 p-3 text-xs text-muted">
                <span className="flex flex-wrap items-center gap-2">
                  {m.usage_rights ? (
                    <Pill tone="success">Rights confirmed</Pill>
                  ) : (
                    <Pill tone="warning" title="CIS will not publish this photo">
                      Rights not confirmed
                    </Pill>
                  )}
                  <span>
                    {m.width} × {m.height} · {bytesLabel(m.bytes)}
                  </span>
                </span>
                <span>
                  {m.uploaded_by.name ?? "Staff"} · <DateText value={m.created_at} />
                </span>
                {canWrite && (
                  <span className="flex flex-wrap gap-2">
                    <Button
                      size="sm"
                      variant="secondary"
                      disabled={update.isPending}
                      onClick={() => update.mutate({ id: m.id, body: { usage_rights: !m.usage_rights } })}
                    >
                      <ShieldCheck size={13} /> {m.usage_rights ? "Withdraw rights" : "Confirm rights"}
                    </Button>
                    <Button size="sm" variant="ghost" disabled={update.isPending} onClick={() => setRemoving(m)} ariaLabel="Remove photo">
                      <Trash2 size={13} /> Remove
                    </Button>
                  </span>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
      <ConfirmDialog
        open={removing !== null}
        onClose={() => setRemoving(null)}
        onConfirm={() => removing && update.mutate({ id: removing.id, body: { status: "removed" } })}
        busy={update.isPending}
        error={mediaError(update.error)}
        danger
        title="Remove photo"
        confirmLabel="Remove"
        message="Remove this photo? CIS stops using it. This cannot be undone: to use it again, upload it again."
      />
    </section>
  );
}

const MEDIA_FRIENDLY: Record<string, string> = {
  media_exists: "This photo is already on the record.",
  too_many_photos: "This record has the most photos allowed. Remove one first.",
  unsupported_image: "Use a JPEG, PNG or WebP photo.",
  content_type_mismatch: "The file's type does not match its contents. Save it again as JPEG, PNG or WebP.",
  image_too_large: "The photo is too large. Use a smaller photo.",
  image_dimensions: "The photo's width or height is outside what Booking accepts.",
  animated_image: "Animated images are not photos. Use a still image.",
  media_removed: "This photo was removed. Upload it again instead.",
  media_storage_unavailable: "Photo storage is not available right now. Try again later.",
  body_too_large: "The photo is too large to send. Use a smaller photo.",
};

function mediaError(err: unknown): string | null {
  if (!err) return null;
  const code = (err as { code?: unknown }).code;
  if (typeof code === "string" && MEDIA_FRIENDLY[code]) return MEDIA_FRIENDLY[code];
  return supplyError(err);
}
