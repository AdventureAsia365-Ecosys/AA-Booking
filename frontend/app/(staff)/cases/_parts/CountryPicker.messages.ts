// app/(staff)/cases/_parts/CountryPicker.messages.ts
// AA-1128 (B5): the English and Vietnamese text of CountryPicker.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Selected countries": "Selected countries",
    "Remove {0}": (...a: unknown[]) => `Remove ${a[0]}`,
    "Add a country": "Add a country",
    "Loading countries…": "Loading countries…",
    "Countries could not load": "Countries could not load",
    "At most {0} countries": (...a: unknown[]) => `At most ${a[0]} countries`,
    "No country is set up in Booking yet": "No country is set up in Booking yet",
    "Add a country…": "Add a country…",
  },
  {
    "Selected countries": "Quốc gia đã chọn",
    "Remove {0}": (...a: unknown[]) => `Xoá ${a[0]}`,
    "Add a country": "Thêm quốc gia",
    "Loading countries…": "Đang tải quốc gia…",
    "Countries could not load": "Không tải được quốc gia",
    "At most {0} countries": (...a: unknown[]) => `Tối đa ${a[0]} quốc gia`,
    "No country is set up in Booking yet": "Booking chưa thiết lập quốc gia nào",
    "Add a country…": "Thêm quốc gia…",
  },
  ),
);
