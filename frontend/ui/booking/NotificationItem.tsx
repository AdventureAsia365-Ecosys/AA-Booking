// ui/booking/NotificationItem.tsx
// AA-683 (fe-dashboard) — the parts a notification row shares between the bell popover and /notifications
// (UI spec §4.3; Figma NOTIFICATION, OVERVIEW-1): a round icon by type (gold when unread, grey when read)
// and the message with case codes in bold. The message is the plain text the API stored; codes are found
// by pattern, so no markup is ever trusted from the database.

import { BadgeCheck, Bell, Handshake, Inbox, Repeat, UserCheck } from "lucide-react";

const TYPE_ICON: Record<string, React.ComponentType<{ size?: number }>> = {
  "trip_case.new_inquiry": Inbox,
  "trip_case.assigned": UserCheck,
  "trip_case.status_changed": Repeat,
  "proposal.approved": BadgeCheck,
  "b2b.on_request": Handshake,
};

export function NotificationIcon({ type, read, size = 40 }: { type: string; read: boolean; size?: number }) {
  const Icon = TYPE_ICON[type] ?? Bell;
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

/** Human codes Booking issues (TC-000123, CUS-000123) and proposal refs (V2). */
const CODE_RE = /\b((?:TC|CUS)-\d{3,})\b/g;

export function NotificationMessage({ text, className = "" }: { text: string; className?: string }) {
  const parts = text.split(CODE_RE);
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
