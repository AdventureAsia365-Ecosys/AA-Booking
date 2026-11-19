// app/(staff)/suppliers/rate-sheets/[id]/page.messages.ts
// AA-1128 (B5): the English and Vietnamese text of page.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Rate sheet review": "Rate sheet review",
    "Check an uploaded rate sheet, then publish it. Publishing needs a second person (maker-checker).": "Check an uploaded rate sheet, then publish it. Publishing needs a second person (maker-checker).",
    "Validation errors per sheet and row, a preview of the parsed rates, and Publish for someone other than the uploader (ADR 0005 §7–8).": "Validation errors per sheet and row, a preview of the parsed rates, and Publish for someone other than the uploader (ADR 0005 §7–8).",
    "Validation": "Validation",
    "Preview rows": "Preview rows",
    "History": "History",
    "Rows": "Rows",
    "Publish": "Publish",
    "Upload": "Upload",
    "Suppliers": "Suppliers",
    "Rate sheet": "Rate sheet",
  },
  {
    "Rate sheet review": "Xem xét bảng giá",
    "Check an uploaded rate sheet, then publish it. Publishing needs a second person (maker-checker).": "Kiểm tra bảng giá đã tải lên, rồi công bố. Việc công bố cần người thứ hai (người nhập và người duyệt khác nhau).",
    "Validation errors per sheet and row, a preview of the parsed rates, and Publish for someone other than the uploader (ADR 0005 §7–8).": "Lỗi kiểm tra theo từng trang và dòng, xem trước các giá đã đọc, và nút Công bố cho người khác với người tải lên (ADR 0005 §7–8).",
    "Validation": "Kiểm tra",
    "Preview rows": "Xem trước các dòng",
    "History": "Lịch sử",
    "Rows": "Dòng",
    "Publish": "Công bố",
    "Upload": "Tải lên",
    "Suppliers": "Nhà cung cấp",
    "Rate sheet": "Bảng giá",
  },
  ),
);
