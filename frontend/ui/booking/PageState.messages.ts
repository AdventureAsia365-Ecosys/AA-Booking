// ui/booking/PageState.messages.ts
// AA-1128 (B5): the English and Vietnamese text of PageState.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Loading": "Loading",
    "You do not have access": "You do not have access",
    "Your Booking role cannot open {0}. Ask a Booking admin if you need it.": (...a: unknown[]) => `Your Booking role cannot open ${a[0]}. Ask a Booking admin if you need it.`,
    "Not found": "Not found",
    "We could not find {0}. It may have been deleted, or the link is wrong.": (...a: unknown[]) => `We could not find ${a[0]}. It may have been deleted, or the link is wrong.`,
    "Could not load {0}.": (...a: unknown[]) => `Could not load ${a[0]}.`,
  },
  {
    "Loading": "Đang tải",
    "You do not have access": "Bạn không có quyền truy cập",
    "Your Booking role cannot open {0}. Ask a Booking admin if you need it.": (...a: unknown[]) => `Vai trò Booking của bạn không mở được ${a[0]}. Hãy hỏi quản trị viên Booking nếu bạn cần.`,
    "Not found": "Không tìm thấy",
    "We could not find {0}. It may have been deleted, or the link is wrong.": (...a: unknown[]) => `Không tìm thấy ${a[0]}. Có thể nó đã bị xoá, hoặc đường dẫn sai.`,
    "Could not load {0}.": (...a: unknown[]) => `Không tải được ${a[0]}.`,
  },
  ),
);
