// app/(staff)/catalog/trip-plans/_parts/UseForTripCaseModal.messages.ts
// AA-1128 (B5): the English and Vietnamese text of UseForTripCaseModal.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "item": "item",
    "V{0} added to {1} from this plan": (...a: unknown[]) => `V${a[0]} added to ${a[1]} from this plan`,
    "Use for trip case": "Use for trip case",
    "Cancel": "Cancel",
    "Adding…": "Adding…",
    "Add version": "Add version",
    "The proposal starts with the {0} {1} of": (...a: unknown[]) => `The proposal starts with the ${a[0]} ${a[1]} of`,
    ", titled from CIS. It is not priced: add cost lines and price it in the builder.": ", titled from CIS. It is not priced: add cost lines and price it in the builder.",
    "Trip case code": "Trip case code",
    "The plan's days become a new version of that case; its earlier versions stay.": "The plan's days become a new version of that case; its earlier versions stay.",
    "Enter a case code like TC-000123.": "Enter a case code like TC-000123.",
    "No open trip case has that code.": "No open trip case has that code.",
    "That trip case is closed (completed or cancelled). Reopen it first, or create a new case.": "That trip case is closed (completed or cancelled). Reopen it first, or create a new case.",
    "day items": "day items",
  },
  {
    "item": "mục",
    "V{0} added to {1} from this plan": (...a: unknown[]) => `Đã thêm V${a[0]} vào ${a[1]} từ kế hoạch này`,
    "Use for trip case": "Dùng cho hồ sơ chuyến đi",
    "Cancel": "Huỷ",
    "Adding…": "Đang thêm…",
    "Add version": "Thêm phiên bản",
    "The proposal starts with the {0} {1} of": (...a: unknown[]) => `Đề xuất bắt đầu với ${a[0]} ${a[1]} của`,
    ", titled from CIS. It is not priced: add cost lines and price it in the builder.": ", tiêu đề lấy từ CIS. Chưa có giá: hãy thêm dòng chi phí và tính giá trong trình soạn.",
    "Trip case code": "Mã hồ sơ chuyến đi",
    "The plan's days become a new version of that case; its earlier versions stay.": "Các ngày của kế hoạch trở thành phiên bản mới của hồ sơ đó; các phiên bản trước vẫn được giữ.",
    "Enter a case code like TC-000123.": "Nhập mã hồ sơ dạng TC-000123.",
    "No open trip case has that code.": "Không có hồ sơ chuyến đi đang mở nào có mã đó.",
    "That trip case is closed (completed or cancelled). Reopen it first, or create a new case.": "Hồ sơ chuyến đi đó đã đóng (hoàn thành hoặc đã huỷ). Hãy mở lại trước, hoặc tạo hồ sơ mới.",
    "day items": "mục theo ngày",
  },
  ),
);
