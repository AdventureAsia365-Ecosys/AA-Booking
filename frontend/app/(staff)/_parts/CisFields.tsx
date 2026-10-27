// app/(staff)/_parts/CisFields.tsx — AA-996: the CIS-owned part of a Figma form (ADD / EDIT COUNTRY, ADD / EDIT
// DESTINATION, ADD NEW / EDIT ACTIVITY, CREATE NEW / EDIT TEMPLATE; docs/figma-coverage.md). The form keeps the Figma
// layout, but the content fields are read-only, tagged "From CIS", filled from what Booking already reads (catalog
// views, CIS supply content) and followed by "Edit in CIS ↗" to that record's CIS admin page (ADR 0002, O7). The
// Booking-owned commercial fields of the same form stay editable below this block.

import { Lock } from "lucide-react";
import { FromCisTag, ManagedInCisLink } from "@/ui";

/** A read-only field drawn like a disabled input (label above, lock icon), `aria-readonly`. */
export function CisField({
  label,
  children,
  multiline = false,
  wide = false,
}: {
  label: string;
  children: React.ReactNode;
  /** Long text (description, summary): wraps and keeps line breaks, capped in height with its own scroll. */
  multiline?: boolean;
  wide?: boolean;
}) {
  const empty = children === null || children === undefined || children === "";
  return (
    <div className={`flex min-w-0 flex-col gap-1.5 ${wide ? "sm:col-span-2" : ""}`}>
      <span className="flex items-center gap-1.5 text-[13px] font-medium text-ink">
        {label}
        <Lock size={12} className="text-muted2" aria-label="Read-only, from CIS" />
      </span>
      <div
        aria-readonly="true"
        className={`rounded-md border border-line bg-panel px-3 text-[13px] text-body ${
          multiline ? "max-h-40 overflow-y-auto whitespace-pre-line break-words py-2" : "flex min-h-10 items-center truncate"
        }`}
      >
        {empty ? <span className="text-muted2">Not set in CIS</span> : children}
      </div>
    </div>
  );
}

/**
 * The "From CIS" block of a form: a heading with the tag, an optional explanation, the read-only fields and the
 * "Edit in CIS ↗" link (`editPath` is a CIS admin path; leave it out when the viewer cannot edit content in CIS or the
 * record has no CIS page).
 */
export function CisFieldset({
  title = "Content",
  description,
  editPath,
  editLabel = "Edit in CIS",
  children,
}: {
  title?: string;
  description?: React.ReactNode;
  editPath?: string | null;
  editLabel?: string;
  children?: React.ReactNode;
}) {
  return (
    <section className="flex flex-col gap-3 rounded-lg border border-line p-4" aria-label={`${title} from CIS`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="flex items-center gap-2">
          <span className="text-[11px] font-semibold uppercase tracking-[0.08em] text-muted">{title}</span>
          <FromCisTag />
        </span>
        {editPath && <ManagedInCisLink path={editPath} label={editLabel} />}
      </div>
      {description && <p className="text-xs text-muted">{description}</p>}
      {children && <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">{children}</div>}
    </section>
  );
}
