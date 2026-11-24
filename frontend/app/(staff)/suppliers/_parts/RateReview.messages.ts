// app/(staff)/suppliers/_parts/RateReview.messages.ts
// AA-1153: the English and Vietnamese text of RateReview.tsx (kit core/i18n.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "No business check found anything to look at.": "No business check found anything to look at.",
    "Business checks": "Business checks",
    "Error": "Error",
    "Warning": "Warning",
    " row {0}": (...a: unknown[]) => ` row ${a[0]}`,
    "Against {0}: {1} added, {2} changed, {3} removed": (...a: unknown[]) => `Against ${a[0]}: ${a[1]} added, ${a[2]} changed, ${a[3]} removed`,
    "No previous contract of this supplier: every rate is new ({0}).": (...a: unknown[]) => `No previous contract of this supplier: every rate is new (${a[0]}).`,
    "… and {0} more": (...a: unknown[]) => `… and ${a[0]} more`,
    "Not in this contract any more:": "Not in this contract any more:",
    "The rate review could not be loaded.": "The rate review could not be loaded.",
    "I checked these warnings with the supplier's sheet.": "I checked these warnings with the supplier's sheet.",
  },
  {
    "No business check found anything to look at.": "Các bước kiểm tra nghiệp vụ không thấy gì cần xem lại.",
    "Business checks": "Kiểm tra nghiệp vụ",
    "Error": "Lỗi",
    "Warning": "Cảnh báo",
    " row {0}": (...a: unknown[]) => ` dòng ${a[0]}`,
    "Against {0}: {1} added, {2} changed, {3} removed": (...a: unknown[]) => `So với ${a[0]}: thêm ${a[1]}, đổi ${a[2]}, bỏ ${a[3]}`,
    "No previous contract of this supplier: every rate is new ({0}).": (...a: unknown[]) => `Nhà cung cấp này chưa có hợp đồng trước: mọi giá đều mới (${a[0]}).`,
    "… and {0} more": (...a: unknown[]) => `… và ${a[0]} dòng nữa`,
    "Not in this contract any more:": "Không còn trong hợp đồng này:",
    "The rate review could not be loaded.": "Không tải được phần xem lại giá.",
    "I checked these warnings with the supplier's sheet.": "Tôi đã đối chiếu các cảnh báo này với bảng giá của nhà cung cấp.",
  },
  ),
);
