// ui/booking/notificationIcons.ts
// AA-996 (Figma NOTIFICATION, OVERVIEW-1, UI spec §4.3) — one icon per notification type, so the bell popover, the
// Notifications page and the Settings preference rows tell a payment from a trip check at a glance. A type not listed
// here falls back to the bell (NotificationItem.tsx). Kept apart from notifications.ts so that module stays free of
// React components (its pure helpers run under Node's test runner).

import {
  AlertTriangle,
  Archive,
  BadgeCheck,
  Ban,
  CalendarClock,
  ClipboardCheck,
  Coins,
  CreditCard,
  Handshake,
  Hourglass,
  Inbox,
  MapPinCheck,
  MapPinOff,
  PhoneOff,
  Receipt,
  ReceiptText,
  Repeat,
  Undo2,
  UserCheck,
  Wallet,
  type LucideIcon,
} from "lucide-react";

export const NOTIFICATION_TYPE_ICON: Record<string, LucideIcon> = {
  "trip_case.new_inquiry": Inbox,
  "trip_case.assigned": UserCheck,
  "trip_case.status_changed": Repeat,
  "trip_case.gate_blocked": AlertTriangle,
  "proposal.approved": BadgeCheck,
  "tour.retired": MapPinOff,
  "tour.reinstated": MapPinCheck,
  "b2b.on_request": Handshake,
  "trip_check.due": ClipboardCheck,
  "supplier_booking.waiting": PhoneOff,
  "supplier_booking.cancel_with_supplier": Ban,
  "payment.recorded": Wallet,
  "payment.due_soon": CalendarClock,
  "invoice.overdue": Receipt,
  "invoice.overpaid": Coins,
  "invoice.balance_reopened": Undo2,
  "invoice.case_cancelled": ReceiptText,
  "supplier_bill.due": Hourglass,
  "supplier_bill.approved": CreditCard,
  "retention.held": Archive,
};
