// ui/booking/NotificationPopover.messages.ts
// AA-1128 (B5): the English and Vietnamese text of NotificationPopover.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Notifications, {0} unread": (...a: unknown[]) => `Notifications, ${a[0]} unread`,
    "Notifications": "Notifications",
    "Mark all as read": "Mark all as read",
    "Notifications are not available right now.": "Notifications are not available right now.",
    "Loading…": "Loading…",
    "You are all caught up.": "You are all caught up.",
    "View all notifications": "View all notifications",
  },
  {
    "Notifications, {0} unread": (...a: unknown[]) => `Thông báo, ${a[0]} chưa đọc`,
    "Notifications": "Thông báo",
    "Mark all as read": "Đánh dấu tất cả là đã đọc",
    "Notifications are not available right now.": "Thông báo hiện không dùng được.",
    "Loading…": "Đang tải…",
    "You are all caught up.": "Bạn đã xem hết.",
    "View all notifications": "Xem mọi thông báo",
  },
  ),
);
