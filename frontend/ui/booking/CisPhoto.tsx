"use client";
// ui/booking/CisPhoto.tsx
// AA-679 — a CIS catalog photo (catalog.v_tour.cover_photo_path / v_tour_photo.path / v_destination_photo.path), served
// by the CIS public API: GET {CIS_MEDIA_URL}{path}?size=small|large (contract catalog.tour.v1), which redirects to the
// CIS photos CDN. A plain lazy <img>: the image is already sized by CIS, so next/image optimisation (and a
// remotePatterns entry) would only add a hop.
// AA-996 (Figma audit items 2 and 3): no photo, or a photo that fails to load, renders the same neutral tile of the
// same box (a soft panel with an optional short label such as an ISO code), never the browser's broken-image glyph or
// an "image off" icon.

import { useState } from "react";
import { CIS_MEDIA_URL } from "@/lib/config";

export function cisPhotoUrl(path: string, size: "small" | "large" = "small"): string {
  return `${CIS_MEDIA_URL}${path.startsWith("/") ? "" : "/"}${path}?size=${size}`;
}

export function CisPhoto({
  path,
  alt,
  size = "small",
  className = "",
  fallbackLabel,
}: {
  path: string | null | undefined;
  alt: string;
  size?: "small" | "large";
  className?: string;
  /** Short text on the neutral tile when there is no photo (an ISO code, "No photo"); empty tile when omitted. */
  fallbackLabel?: string;
}) {
  // The path that failed to load, so a new path (a re-render with another photo) is tried again without an effect.
  const [failed, setFailed] = useState<string | null>(null);
  if (!path || failed === path) {
    return (
      <span
        role={fallbackLabel ? "img" : undefined}
        aria-label={fallbackLabel ? alt || fallbackLabel : undefined}
        aria-hidden={fallbackLabel ? undefined : true}
        data-cis-photo="none"
        className={`grid place-items-center overflow-hidden bg-panel bg-gradient-to-br from-panel to-line/60 text-muted2 ${className}`}
      >
        {fallbackLabel && <span className="select-none px-1 text-center text-[11px] font-semibold uppercase tracking-[0.08em]">{fallbackLabel}</span>}
      </span>
    );
  }
  return (
    // eslint-disable-next-line @next/next/no-img-element -- CIS-served, already sized (see header)
    <img
      src={cisPhotoUrl(path, size)}
      alt={alt}
      loading="lazy"
      decoding="async"
      onError={() => setFailed(path)}
      className={`bg-panel object-cover ${className}`}
    />
  );
}
