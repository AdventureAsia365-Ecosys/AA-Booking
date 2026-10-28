// ui/booking/NotificationItem.tsx
// AA-683 (fe-dashboard) — the parts a notification row shares between the bell popover and /notifications
// (UI spec §4.3; Figma NOTIFICATION, OVERVIEW-1): a round icon by type (gold when unread, grey when read)
// and the message with case codes in bold. The message is the plain text the API stored; codes are found
// by pattern, so no markup is ever trusted from the database.

import { NOTIFICATION_TYPE_ICON } from "./notificationIcons";
import { Bell } from "lucide-react";
import { localizeIsoDates } from "./dates";

export function NotificationIcon({ type, read, size = 40 }: { type: string; read: boolean; size?: number }) {
  // AA-996: every staff type has its own icon (notificationIcons.ts); an unknown type gets the bell.
  const Icon = NOTIFICATION_TYPE_ICON[type] ?? Bell;
  return (
    <span
      aria-hidden="true"
      style={{ width: size, height: size }}
      className={`grid shrink-0 place-items-center rounded-full ${
        read ? "bg-line2 text-muted" : "bg-accent text-on-accent"
      }`}
    >
      <Icon size={Math.round(size * 0.45)} />
    </span>
  );
}

/** Human codes Booking issues (TC-000123, CUS-000123; AA-996: INV-, PMT-, BIL-, SB- too) and proposal refs (V2). */
const CODE_RE = /\b((?:TC|CUS|INV|PMT|BIL|SB)-\d{3,})\b/g;

export function NotificationMessage({ text, className = "" }: { text: string; className?: string }) {
  // AA-996: the API stores dates in the text as ISO ("next due 2026-11-06"); show them like every other date.
  const parts = localizeIsoDates(text).split(CODE_RE);
  return (
    <span className={className}>
      {parts.map((part, i) =>
        i % 2 === 1 ? (
          <strong key={i} className="whitespace-nowrap font-semibold text-ink">
            {part}
          </strong>
        ) : (
          part
        ),
      )}
    </span>
  );
}
