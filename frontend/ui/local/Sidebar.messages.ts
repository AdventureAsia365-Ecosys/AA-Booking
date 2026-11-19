// ui/local/Sidebar.messages.ts
// AA-1128 (B5): the English and Vietnamese text of Sidebar.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Adventure Asia Booking: Dashboard": "Adventure Asia Booking: Dashboard",
    "Adventure Asia Booking, go to Dashboard": "Adventure Asia Booking, go to Dashboard",
    "Booking": "Booking",
    "Primary": "Primary",
    "Sign out": "Sign out",
    "Expand sidebar ([)": "Expand sidebar ([)",
    "Collapse sidebar ([)": "Collapse sidebar ([)",
    "Expand sidebar": "Expand sidebar",
    "Collapse sidebar": "Collapse sidebar",
    "Collapse": "Collapse",
  },
  {
    "Adventure Asia Booking: Dashboard": "Adventure Asia Booking: Bảng điều khiển",
    "Adventure Asia Booking, go to Dashboard": "Adventure Asia Booking, tới Bảng điều khiển",
    "Booking": "Booking",
    "Primary": "Điều hướng chính",
    "Sign out": "Đăng xuất",
    "Expand sidebar ([)": "Mở rộng thanh bên ([)",
    "Collapse sidebar ([)": "Thu gọn thanh bên ([)",
    "Expand sidebar": "Mở rộng thanh bên",
    "Collapse sidebar": "Thu gọn thanh bên",
    "Collapse": "Thu gọn",
  },
  ),
);
