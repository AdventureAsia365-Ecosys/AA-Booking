"use client";
// app/(staff)/catalog/countries/_parts/CountryPhotos.tsx — AA-996 (Figma COUNTRY DETAILS hero + media gallery; audit
// item 3): the CIS photos of one country, read-only "From CIS". CountryHero is the cover (the first photo of the
// country's best-covered place, else tour: backend app/catalog/geo.py); CountryGallery is the first photos the API
// returns (`photo_items`, cover first), each opening a lightbox with previous / next. No photo = the neutral CisPhoto
// tile with the ISO code, and a line saying photos are added in CIS. Booking never uploads country photos.

import { useEffect, useState } from "react";
import { ChevronLeft, ChevronRight, MapPin } from "lucide-react";
import type { CountryDetail, CountryPhoto } from "@/lib/api/geo";
import { CisPhoto, FromCisTag, IconButton, LinkButton, ManagedInCisLink, Modal, Pressable, Section } from "@/ui";

/** CIS admin page of the photo library (per-country Drive folders matched to places and tours). */
export const CIS_PHOTOS_PATH = "/admin/photos";

export function CountryHero({ c }: { c: CountryDetail }) {
  // The cover path that failed to load: the caption then says there is no photo instead of naming one not on screen.
  // Kept per path (like CisPhoto), so a new cover after a refetch is described again without an effect.
  const [failedCover, setFailedCover] = useState<string | null>(null);
  const cover = c.cover_photo_path && failedCover !== c.cover_photo_path ? c.cover_photo_path : null;
  const coverCaption = cover ? c.photo_items.find((p) => p.path === cover)?.caption : undefined;
  return (
    <figure className="flex flex-col gap-1.5">
      <CisPhoto
        path={c.cover_photo_path}
        size="large"
        alt={`${c.name} cover photo`}
        fallbackLabel={c.country_code}
        onLoadError={() => setFailedCover(c.cover_photo_path)}
        className="aspect-[16/5] max-h-[280px] w-full rounded-lg border border-line"
      />
      <figcaption className="text-xs text-muted" data-testid="country-hero-caption">
        {cover ? `${coverCaption ? `${coverCaption} · ` : ""}Cover from CIS` : `CIS has no photo for ${c.name} yet.`}
      </figcaption>
    </figure>
  );
}

export function CountryGallery({ c, canOpenCis, onPlace }: { c: CountryDetail; canOpenCis: boolean; onPlace: (destinationId: string) => void }) {
  const [open, setOpen] = useState<number | null>(null);
  const items = c.photo_items;
  return (
    <Section
      title="Photos"
      description={
        items.length < c.photos
          ? `The first ${items.length} of ${c.photos} CIS photos of its places and tours.`
          : "CIS photos of its places and tours."
      }
      actions={
        <span className="flex items-center gap-3">
          <FromCisTag />
          {canOpenCis && <ManagedInCisLink path={CIS_PHOTOS_PATH} label="Open CIS photos" />}
        </span>
      }
    >
      {items.length === 0 ? (
        <p className="text-[13px] text-muted2" data-testid="country-photos-empty">
          No CIS photos for {c.name} yet. Photos are added and matched to places in CIS; they show here as soon as CIS
          publishes them.
        </p>
      ) : (
        <ul className="grid grid-cols-2 gap-3 sm:grid-cols-4 xl:grid-cols-6" data-testid="country-photos">
          {items.map((p, i) => (
            <li key={p.path} className="min-w-0">
              <Pressable
                onClick={() => setOpen(i)}
                className="overflow-hidden rounded-md"
                aria-label={`Open photo ${i + 1}${p.caption ? `: ${p.caption}` : ""}`}
              >
                <CisPhoto path={p.path} alt="" className="aspect-[4/3] w-full transition-transform hover:scale-[1.03]" />
              </Pressable>
              {p.caption && <p className="mt-1 truncate text-xs text-muted">{p.caption}</p>}
            </li>
          ))}
        </ul>
      )}
      {open !== null && items[open] && (
        <Lightbox
          items={items}
          index={open}
          name={c.name}
          onIndex={setOpen}
          onClose={() => setOpen(null)}
          onPlace={(id) => {
            setOpen(null);
            onPlace(id);
          }}
        />
      )}
    </Section>
  );
}

function Lightbox({
  items,
  index,
  name,
  onIndex,
  onClose,
  onPlace,
}: {
  items: CountryPhoto[];
  index: number;
  name: string;
  onIndex: (i: number) => void;
  onClose: () => void;
  onPlace: (destinationId: string) => void;
}) {
  const p = items[index];
  const many = items.length > 1;
  const prev = () => onIndex((index - 1 + items.length) % items.length);
  const next = () => onIndex((index + 1) % items.length);
  useEffect(() => {
    if (!many) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "ArrowLeft") onIndex((index - 1 + items.length) % items.length);
      if (e.key === "ArrowRight") onIndex((index + 1) % items.length);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [many, index, items.length, onIndex]);
  return (
    <Modal open onClose={onClose} width={960} title={`${name} · photo ${index + 1} of ${items.length}`}>
      <div className="flex flex-col gap-3">
        <div className="relative">
          <CisPhoto
            key={p.path}
            path={p.path}
            size="large"
            alt={p.caption ? `${p.caption}, ${name}` : `${name} photo ${index + 1}`}
            fallbackLabel="No photo"
            fit="contain"
            className="max-h-[70vh] min-h-[240px] w-full rounded-md"
          />
          {many && (
            <>
              <NavButton side="left" label="Previous photo" onClick={prev}>
                <ChevronLeft size={18} />
              </NavButton>
              <NavButton side="right" label="Next photo" onClick={next}>
                <ChevronRight size={18} />
              </NavButton>
            </>
          )}
        </div>
        <div className="flex flex-wrap items-center justify-between gap-2 text-[13px]">
          <span className="min-w-0 truncate text-body">{p.caption ?? "Tour photo"}</span>
          {p.destination_id && (
            <LinkButton onClick={() => onPlace(p.destination_id!)} style={{ gap: 4 }}>
              <MapPin size={13} /> Open destination
            </LinkButton>
          )}
        </div>
      </div>
    </Modal>
  );
}

function NavButton({ side, label, onClick, children }: { side: "left" | "right"; label: string; onClick: () => void; children: React.ReactNode }) {
  return (
    <IconButton
      variant="overlay"
      size="lg"
      label={label}
      title=""
      onClick={onClick}
      className={`absolute top-1/2 -translate-y-1/2 ${side === "left" ? "left-2" : "right-2"}`}
    >
      {children}
    </IconButton>
  );
}
