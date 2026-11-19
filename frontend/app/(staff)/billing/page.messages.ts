// app/(staff)/billing/page.messages.ts
// AA-1128 (B5): the English and Vietnamese text of page.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Supplier bills": "Supplier bills",
    "Bills suppliers sent AA, matched to confirmed supplier bookings. Another person approves each bill; an admin records the payouts.": "Bills suppliers sent AA, matched to confirmed supplier bookings. Another person approves each bill; an admin records the payouts.",
    "No access": "No access",
    "Supplier bills need the bill reader permission (admin, sales or contracting).": "Supplier bills need the bill reader permission (admin, sales or contracting).",
  },
  {
    "Supplier bills": "Hoá đơn nhà cung cấp",
    "Bills suppliers sent AA, matched to confirmed supplier bookings. Another person approves each bill; an admin records the payouts.": "Hoá đơn nhà cung cấp gửi AA, được đối chiếu với các đặt chỗ đã xác nhận. Người khác duyệt từng hoá đơn; quản trị viên ghi nhận các khoản chi trả.",
    "No access": "Không có quyền truy cập",
    "Supplier bills need the bill reader permission (admin, sales or contracting).": "Hoá đơn nhà cung cấp cần quyền đọc hoá đơn (quản trị, bán hàng hoặc hợp đồng).",
  },
  ),
);
