// app/(staff)/suppliers/_parts/useSupplierActions.messages.ts
// AA-1128 (B5): the English and Vietnamese text of useSupplierActions.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Deactivate supplier": "Deactivate supplier",
    "Activate supplier": "Activate supplier",
    "Deactivate": "Deactivate",
    "Activate": "Activate",
    "Reason (optional)": "Reason (optional)",
    "e.g. closed for renovation": "e.g. closed for renovation",
    "? It stays on record with its contracts and history, but no new contract can be added until it is activated again. A supplier with a published contract in force or starting later cannot be deactivated.": "? It stays on record with its contracts and history, but no new contract can be added until it is activated again. A supplier with a published contract in force or starting later cannot be deactivated.",
    "? Contracting can add and publish contracts for it again.": "? Contracting can add and publish contracts for it again.",
    "You created this supplier. Activating it yourself is recorded as a business-owner self-approval.": "You created this supplier. Activating it yourself is recorded as a business-owner self-approval.",
  },
  {
    "Deactivate supplier": "Ngưng hoạt động nhà cung cấp",
    "Activate supplier": "Kích hoạt nhà cung cấp",
    "Deactivate": "Ngưng hoạt động",
    "Activate": "Kích hoạt",
    "Reason (optional)": "Lý do (không bắt buộc)",
    "e.g. closed for renovation": "ví dụ: đóng cửa để sửa chữa",
    "? It stays on record with its contracts and history, but no new contract can be added until it is activated again. A supplier with a published contract in force or starting later cannot be deactivated.": "? Nhà cung cấp vẫn được lưu cùng hợp đồng và lịch sử, nhưng không thể thêm hợp đồng mới cho tới khi được kích hoạt lại. Nhà cung cấp có hợp đồng đã công bố đang áp dụng hoặc sắp bắt đầu thì không ngưng hoạt động được.",
    "? Contracting can add and publish contracts for it again.": "? Bộ phận hợp đồng có thể thêm và công bố hợp đồng cho nó trở lại.",
    "You created this supplier. Activating it yourself is recorded as a business-owner self-approval.": "Bạn đã tạo nhà cung cấp này. Tự kích hoạt sẽ được ghi nhận là chủ doanh nghiệp tự duyệt.",
  },
  ),
);
