// ui/booking/CisPhoto.tsx
// AA-679 — a CIS catalog photo (catalog.v_tour.cover_photo_path / v_tour_photo.path), served by the CIS public
// API: GET {CIS_MEDIA_URL}{path}?size=small|large (contract catalog.tour.v1). A plain lazy <img>: the image is
// already sized by CIS, so next/image optimisation (and a remotePatterns entry) would only add a hop. No photo
// renders a neutral placeholder of the same box.

import { ImageOff } from "lucide-react";
import { CIS_MEDIA_URL } from "@/lib/config";

export function cisPhotoUrl(path: string, size: "small" | "large" = "small"): string {
  return `${CIS_MEDIA_URL}${path.startsWith("/") ? "" : "/"}${path}?size=${size}`;
}

export function CisPhoto({
  path,
  alt,
  size = "small",
  className = "",
}: {
  path: string | null | undefined;
  alt: string;
  size?: "small" | "large";
  className?: string;
}) {
  if (!path) {
    return (
      <span aria-hidden="true" className={`grid place-items-center bg-panel text-muted2 ${className}`}>
        <ImageOff size={16} />
      </span>
    );
  }
  return (
    // eslint-disable-next-line @next/next/no-img-element -- CIS-served, already sized (see header)
    <img src={cisPhotoUrl(path, size)} alt={alt} loading="lazy" decoding="async" className={`bg-panel object-cover ${className}`} />
  );
}
