// app/(staff)/cases/[code]/proposals/[n]/SupplyPicker.messages.ts
// AA-1128 (B5): the English and Vietnamese text of SupplyPicker.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Type 2+ letters of a hotel, room or service": "Type 2+ letters of a hotel, room or service",
    "Searching…": "Searching…",
    "No active room type or service matches.": "No active room type or service matches.",
    "Room · ": "Room · ",
    "Service · ": "Service · ",
  },
  {
    "Type 2+ letters of a hotel, room or service": "Gõ từ 2 chữ cái của khách sạn, phòng hoặc dịch vụ",
    "Searching…": "Đang tìm…",
    "No active room type or service matches.": "Không có loại phòng hoặc dịch vụ đang hoạt động nào khớp.",
    "Room · ": "Phòng · ",
    "Service · ": "Dịch vụ · ",
  },
  ),
);
