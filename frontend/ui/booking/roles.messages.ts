// ui/booking/roles.messages.ts
// AA-1128 follow-up (S285): the Vietnamese of the staff role names shown with `tx()`: the Booking roles
// (lib/rbac.ts ROLE_LABEL) and the CIS roles a staff row carries as information (lib/rbac.ts CIS_ROLE_LABEL, worded as
// the CIS admin's own role names). ui/booking/viOptionLabels.test.mjs checks every role has its Vietnamese name.
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
    {
      Admin: "Admin",
      Sales: "Sales",
      "Product & operations": "Product & operations",
      "Content creator": "Content creator",
      Finance: "Finance",
      Administrator: "Administrator",
      "Content Team": "Content Team",
      "Viewer (read-only)": "Viewer (read-only)",
      "Content Lead": "Content Lead",
      "Tenant Content QA": "Tenant Content QA",
      "Customer Success": "Customer Success",
      Support: "Support",
      "AI Operations": "AI Operations",
    },
    {
      Admin: "Quản trị",
      Sales: "Bán hàng",
      "Product & operations": "Sản phẩm & điều hành",
      "Content creator": "Người tạo nội dung",
      Finance: "Tài chính",
      Administrator: "Quản trị viên",
      "Content Team": "Nhóm nội dung",
      "Viewer (read-only)": "Người xem (chỉ đọc)",
      "Content Lead": "Trưởng nhóm nội dung",
      "Tenant Content QA": "Kiểm duyệt nội dung tenant",
      "Customer Success": "Chăm sóc khách hàng",
      Support: "Hỗ trợ",
      "AI Operations": "Vận hành AI",
    },
  ),
);
