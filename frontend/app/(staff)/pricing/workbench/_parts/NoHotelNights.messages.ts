// app/(staff)/pricing/workbench/_parts/NoHotelNights.messages.ts
// AA-1149: the English and Vietnamese text of NoHotelNights.tsx (kit core/i18n.ts). The keys are the English text;
// the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Could not save": "Could not save",
    "Saved": "Saved",
    "Why": "Why",
    "Why do these nights need no hotel?": "Why do these nights need no hotel?",
    "{0} / at least {1} characters": (...a: unknown[]) => `${a[0]} / at least ${a[1]} characters`,
    "Save nights": "Save nights",
    "Can agencies buy it?": "Can agencies buy it?",
    "Night {0}: no hotel needed": (...a: unknown[]) => `Night ${a[0]}: no hotel needed`,
    "A night train or a cruise without a cabin room type needs no hotel; a cruise with a cabin is better added as a hotel room type.": "A night train or a cruise without a cabin room type needs no hotel; a cruise with a cabin is better added as a hotel room type.",
    "Night {0} has no hotel": (...a: unknown[]) => `Night ${a[0]} has no hotel`,
    "Supplier {0} is not active": (...a: unknown[]) => `Supplier ${a[0]} is not active`,
    "Day {0}: the hotel moves with no transfer": (...a: unknown[]) => `Day ${a[0]}: the hotel moves with no transfer`,
  },
  {
    "Could not save": "Không lưu được",
    "Saved": "Đã lưu",
    "Why": "Lý do",
    "Why do these nights need no hotel?": "Vì sao các đêm này không cần khách sạn?",
    "{0} / at least {1} characters": (...a: unknown[]) => `${a[0]} / tối thiểu ${a[1]} ký tự`,
    "Save nights": "Lưu các đêm",
    "Can agencies buy it?": "Đại lý mua được không?",
    "Night {0}: no hotel needed": (...a: unknown[]) => `Đêm ${a[0]}: không cần khách sạn`,
    "A night train or a cruise without a cabin room type needs no hotel; a cruise with a cabin is better added as a hotel room type.": "Tàu đêm hoặc du thuyền không có loại phòng cabin thì không cần khách sạn; du thuyền có cabin nên được thêm như một loại phòng khách sạn.",
    "Night {0} has no hotel": (...a: unknown[]) => `Đêm ${a[0]} chưa có khách sạn`,
    "Supplier {0} is not active": (...a: unknown[]) => `Nhà cung cấp ${a[0]} chưa hoạt động`,
    "Day {0}: the hotel moves with no transfer": (...a: unknown[]) => `Ngày ${a[0]}: đổi khách sạn sang nơi khác mà không có xe đưa đón`,
  },
  ),
);
