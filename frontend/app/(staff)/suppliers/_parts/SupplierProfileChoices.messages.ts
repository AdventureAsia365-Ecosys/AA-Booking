// app/(staff)/suppliers/_parts/SupplierProfileChoices.messages.ts
// AA-1158 part 2 (review S289): the English and Vietnamese text of SupplierProfileChoices.tsx (kit core/i18n.ts). The
// keys are the English text; the phrasebook registers the Vietnamese side for text kept in module-level tables.
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Supplier profile choices": "Supplier profile choices",
    "The choices offered on every supplier's profile. A code never changes; a retired choice stays on the suppliers that hold it and is not offered again.": "The choices offered on every supplier's profile. A code never changes; a retired choice stays on the suppliers that hold it and is not offered again.",
    "Loading the choices": "Loading the choices",
    "Contact roles": "Contact roles",
    "Payment bases": "Payment bases",
    "Payment methods": "Payment methods",
    "Choice saved": "Choice saved",
    "Choice added": "Choice added",
    "English label": "English label",
    "Vietnamese label": "Vietnamese label",
    "Order": "Order",
    "Offered": "Offered",
    "Retired": "Retired",
    "Save": "Save",
    "New code": "New code",
    "code": "code",
    "Add": "Add",
  },
  {
    "Supplier profile choices": "Các lựa chọn hồ sơ nhà cung cấp",
    "The choices offered on every supplier's profile. A code never changes; a retired choice stays on the suppliers that hold it and is not offered again.": "Các lựa chọn hiển thị trên hồ sơ của mọi nhà cung cấp. Mã không bao giờ thay đổi; lựa chọn đã ngừng dùng vẫn giữ ở nhà cung cấp đang có nó và không được đề xuất nữa.",
    "Loading the choices": "Đang tải các lựa chọn",
    "Contact roles": "Vai trò liên hệ",
    "Payment bases": "Hình thức thanh toán",
    "Payment methods": "Phương thức thanh toán",
    "Choice saved": "Đã lưu lựa chọn",
    "Choice added": "Đã thêm lựa chọn",
    "English label": "Nhãn tiếng Anh",
    "Vietnamese label": "Nhãn tiếng Việt",
    "Order": "Thứ tự",
    "Offered": "Đang dùng",
    "Retired": "Đã ngừng dùng",
    "Save": "Lưu",
    "New code": "Mã mới",
    "code": "mã",
    "Add": "Thêm",
  },
  ),
);
