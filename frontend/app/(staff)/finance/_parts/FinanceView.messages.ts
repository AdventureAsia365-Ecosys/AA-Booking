// app/(staff)/finance/_parts/FinanceView.messages.ts
// AA-1128 (B5): the English and Vietnamese text of FinanceView.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Profit": "Profit",
    "Departure costs": "Departure costs",
    "Purchase orders": "Purchase orders",
    "Costs & profit": "Costs & profit",
    "Supplier costs normalised to USD at the approved rates, shared costs of each departure, purchase orders to suppliers and the profit of every trip case, departure and agency.": "Supplier costs normalised to USD at the approved rates, shared costs of each departure, purchase orders to suppliers and the profit of every trip case, departure and agency.",
    "No access": "No access",
    "Costs need the pricing reader permission; profit needs the finance or admin role.": "Costs need the pricing reader permission; profit needs the finance or admin role.",
    "Costs and profit": "Costs and profit",
  },
  {
    "Profit": "Lợi nhuận",
    "Departure costs": "Chi phí chuyến khởi hành",
    "Purchase orders": "Đơn đặt hàng",
    "Costs & profit": "Chi phí & lợi nhuận",
    "Supplier costs normalised to USD at the approved rates, shared costs of each departure, purchase orders to suppliers and the profit of every trip case, departure and agency.": "Chi phí nhà cung cấp quy về USD theo tỷ giá đã duyệt, chi phí dùng chung của từng chuyến khởi hành, đơn đặt hàng gửi nhà cung cấp và lợi nhuận của từng hồ sơ, chuyến khởi hành và đại lý.",
    "No access": "Không có quyền truy cập",
    "Costs need the pricing reader permission; profit needs the finance or admin role.": "Chi phí cần quyền đọc giá; lợi nhuận cần vai trò tài chính hoặc quản trị.",
    "Costs and profit": "Chi phí và lợi nhuận",
  },
  ),
);
