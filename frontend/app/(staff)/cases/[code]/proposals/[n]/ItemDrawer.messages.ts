// app/(staff)/cases/[code]/proposals/[n]/ItemDrawer.messages.ts
// AA-1128 (B5): the English and Vietnamese text of ItemDrawer.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Add item": "Add item",
    "Edit item": "Edit item",
    "Apply": "Apply",
    "Title": "Title",
    "e.g. Hanoi city walk and street food evening": "e.g. Hanoi city walk and street food evening",
    "From day": "From day",
    "To day": "To day",
    "An item can span several days, e.g. a 3-day trek on days 4–6.": "An item can span several days, e.g. a 3-day trek on days 4–6.",
    "Note to the customer": "Note to the customer",
    "Pick-up time, room note, what is arranged for this customer. Tour descriptions come from CIS.": "Pick-up time, room note, what is arranged for this customer. Tour descriptions come from CIS.",
    "Hotel room or service": "Hotel room or service",
    "Room": "Room",
    "Service": "Service",
    "(inactive)": "(inactive)",
    "Remove the room or service": "Remove the room or service",
    "Optional. With no net cost line, saving takes the supplier's rate in force on the item's first day. Picking another room or service drops the lines taken from the previous one's rate.": "Optional. With no net cost line, saving takes the supplier's rate in force on the item's first day. Picking another room or service drops the lines taken from the previous one's rate.",
    "CIS tour": "CIS tour",
    "Detach": "Detach",
    "No CIS tour attached. Picking a tour from the CIS catalog arrives with the catalog endpoint (GET /booking/catalog/tours).": "No CIS tour attached. Picking a tour from the CIS catalog arrives with the catalog endpoint (GET /booking/catalog/tours).",
  },
  {
    "Add item": "Thêm mục",
    "Edit item": "Sửa mục",
    "Apply": "Áp dụng",
    "Title": "Tiêu đề",
    "e.g. Hanoi city walk and street food evening": "ví dụ: Đi bộ phố Hà Nội và buổi tối ẩm thực đường phố",
    "From day": "Từ ngày",
    "To day": "Đến ngày",
    "An item can span several days, e.g. a 3-day trek on days 4–6.": "Một mục có thể kéo dài nhiều ngày, ví dụ chuyến trek 3 ngày vào ngày 4–6.",
    "Note to the customer": "Ghi chú cho khách",
    "Pick-up time, room note, what is arranged for this customer. Tour descriptions come from CIS.": "Giờ đón, ghi chú phòng, những gì đã sắp xếp cho khách này. Mô tả tour lấy từ CIS.",
    "Hotel room or service": "Phòng khách sạn hoặc dịch vụ",
    "Room": "Phòng",
    "Service": "Dịch vụ",
    "(inactive)": "(ngưng hoạt động)",
    "Remove the room or service": "Bỏ phòng hoặc dịch vụ",
    "Optional. With no net cost line, saving takes the supplier's rate in force on the item's first day. Picking another room or service drops the lines taken from the previous one's rate.": "Không bắt buộc. Khi không có dòng chi phí net, lúc lưu sẽ lấy giá nhà cung cấp đang áp dụng vào ngày đầu của mục. Chọn phòng hoặc dịch vụ khác sẽ bỏ các dòng lấy từ giá của lựa chọn trước.",
    "CIS tour": "Tour CIS",
    "Detach": "Gỡ liên kết",
    "No CIS tour attached. Picking a tour from the CIS catalog arrives with the catalog endpoint (GET /booking/catalog/tours).": "Chưa gắn tour CIS. Việc chọn tour từ danh mục CIS sẽ có khi có endpoint danh mục (GET /booking/catalog/tours).",
  },
  ),
);
