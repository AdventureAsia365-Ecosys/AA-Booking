// ui/local/AdminShell.messages.ts
// AA-1128 (B5): the English and Vietnamese text of AdminShell.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "You do not have access to this page": "You do not have access to this page",
    "Your Booking role does not include this page. Ask a Booking admin if you need it.": "Your Booking role does not include this page. Ask a Booking admin if you need it.",
    "Back to the dashboard": "Back to the dashboard",
  },
  {
    "You do not have access to this page": "Bạn không có quyền xem trang này",
    "Your Booking role does not include this page. Ask a Booking admin if you need it.": "Vai trò Booking của bạn không gồm trang này. Hãy hỏi quản trị viên Booking nếu bạn cần.",
    "Back to the dashboard": "Quay lại bảng điều khiển",
  },
  ),
);
