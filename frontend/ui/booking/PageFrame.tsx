// ui/booking/PageFrame.tsx
// AA-683 — the scroll region of every staff page with the kit PageHeader on top. Since AA-994 S4b (root ADR 0016)
// it is the kit core PageFrame (ui/core/PageFrame.tsx, upstreamed from this file in S4a) with the Booking defaults,
// so the pages keep their look: a "wide" 1440 px column (the core default is "full") and the Booking gutter of
// 24 px 32 px (Tailwind `px-8 py-6` before; the core frame pads 28 px 36 px 56 px). The phone rule (ui/local/shell.css)
// and the 768-1199 px compact rule (app/globals.css) still override the gutter through `.aa-admin-main !important`.
//
// AA-994 S6d: `bleed` is the frame of the detail pages that draw an edge-to-edge header band (DetailHeader, the trip
// case and new-case bands) over their body: full width and no gutter, the band and the body pad themselves. It replaced
// their hand-written `<PageFrame bleed>` (lint rule no-raw-main). The phone rule
// still gives these pages the 16 px phone gutter, as it did the raw <main>.

import type { ComponentProps } from "react";
import { PageFrame as KitPageFrame } from "../core/PageFrame";

const BOOKING_GUTTER = { padding: "24px 32px" } as const;
const NO_GUTTER = { padding: 0 } as const;

export function PageFrame({
  width,
  style,
  bleed = false,
  ...rest
}: ComponentProps<typeof KitPageFrame> & {
  /** Full width, no gutter: for a page with an edge-to-edge header band. */
  bleed?: boolean;
}) {
  return (
    <KitPageFrame
      width={width ?? (bleed ? "full" : "wide")}
      style={{ ...(bleed ? NO_GUTTER : BOOKING_GUTTER), ...style }}
      {...rest}
    />
  );
}
