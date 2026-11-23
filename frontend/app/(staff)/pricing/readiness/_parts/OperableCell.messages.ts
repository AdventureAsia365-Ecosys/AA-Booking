// app/(staff)/pricing/readiness/_parts/OperableCell.messages.ts
// AA-1149: the English and Vietnamese text of OperableCell.tsx (kit core/i18n.ts). The keys are the English text;
// the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Ready": "Ready",
    "Warnings": "Warnings",
    "Blocked": "Blocked",
    "Exception": "Exception",
    "until": "until",
    "and {0} more": (...a: unknown[]) => `and ${a[0]} more`,
    "Allow sale…": "Allow sale…",
    "Exception…": "Exception…",
    "Night {0} has no hotel": (...a: unknown[]) => `Night ${a[0]} has no hotel`,
    "Supplier {0} is not active": (...a: unknown[]) => `Supplier ${a[0]} is not active`,
    "Day {0}: the hotel moves with no transfer": (...a: unknown[]) => `Day ${a[0]}: the hotel moves with no transfer`,
  },
  {
    "Ready": "Sẵn sàng",
    "Warnings": "Có cảnh báo",
    "Blocked": "Bị chặn",
    "Exception": "Ngoại lệ",
    "until": "đến",
    "and {0} more": (...a: unknown[]) => `và ${a[0]} mục khác`,
    "Allow sale…": "Cho phép bán…",
    "Exception…": "Ngoại lệ…",
    "Night {0} has no hotel": (...a: unknown[]) => `Đêm ${a[0]} chưa có khách sạn`,
    "Supplier {0} is not active": (...a: unknown[]) => `Nhà cung cấp ${a[0]} chưa hoạt động`,
    "Day {0}: the hotel moves with no transfer": (...a: unknown[]) => `Ngày ${a[0]}: đổi khách sạn sang nơi khác mà không có xe đưa đón`,
  },
  ),
);
