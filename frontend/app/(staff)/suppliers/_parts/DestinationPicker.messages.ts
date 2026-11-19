// app/(staff)/suppliers/_parts/DestinationPicker.messages.ts
// AA-1128 (B5): the English and Vietnamese text of DestinationPicker.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Clear the place": "Clear the place",
    "Type 2+ letters of a city or place": "Type 2+ letters of a city or place",
    "Searching…": "Searching…",
    "No CIS place matches{0}.": (...a: unknown[]) => `No CIS place matches${a[0]}.`,
    " in this country": " in this country",
  },
  {
    "Clear the place": "Xoá địa điểm",
    "Type 2+ letters of a city or place": "Gõ từ 2 chữ cái của thành phố hoặc địa điểm",
    "Searching…": "Đang tìm…",
    "No CIS place matches{0}.": (...a: unknown[]) => `Không có địa điểm CIS nào khớp${a[0]}.`,
    " in this country": " ở quốc gia này",
  },
  ),
);
