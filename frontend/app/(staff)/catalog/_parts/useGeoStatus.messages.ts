// app/(staff)/catalog/_parts/useGeoStatus.messages.ts
// AA-1128 (B5): the English and Vietnamese text of useGeoStatus.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "activated": "activated",
    "deactivated": "deactivated",
    "{0} {1} in Booking.": (...a: unknown[]) => `${a[0]} ${a[1]} in Booking.`,
    "Activate {0}": (...a: unknown[]) => `Activate ${a[0]}`,
    "Deactivate {0}": (...a: unknown[]) => `Deactivate ${a[0]}`,
    "Reason (optional)": "Reason (optional)",
    "e.g. no guides this season": "e.g. no guides this season",
    "Activate": "Activate",
    "in Booking? Staff see it as a place Booking sells again.": "in Booking? Staff see it as a place Booking sells again.",
    "Deactivate": "Deactivate",
    "in Booking? Nothing is deleted: its content stays in CIS, trip cases and suppliers there are kept, and it shows as inactive until it is activated again.": "in Booking? Nothing is deleted: its content stays in CIS, trip cases and suppliers there are kept, and it shows as inactive until it is activated again.",
  },
  {
    "activated": "đã bật",
    "deactivated": "đã tắt",
    "{0} {1} in Booking.": (...a: unknown[]) => `${a[0]} ${a[1]} trong Booking.`,
    "Activate {0}": (...a: unknown[]) => `Kích hoạt ${a[0]}`,
    "Deactivate {0}": (...a: unknown[]) => `Ngưng hoạt động ${a[0]}`,
    "Reason (optional)": "Lý do (không bắt buộc)",
    "e.g. no guides this season": "ví dụ: mùa này không có hướng dẫn viên",
    "Activate": "Kích hoạt",
    "in Booking? Staff see it as a place Booking sells again.": "trong Booking? Nhân viên sẽ thấy đây là nơi Booking bán lại.",
    "Deactivate": "Ngưng hoạt động",
    "in Booking? Nothing is deleted: its content stays in CIS, trip cases and suppliers there are kept, and it shows as inactive until it is activated again.": "trong Booking? Không xoá gì: nội dung vẫn ở CIS, hồ sơ chuyến đi và nhà cung cấp ở đó được giữ, và nó hiện là ngưng hoạt động cho tới khi được kích hoạt lại.",
  },
  ),
);
