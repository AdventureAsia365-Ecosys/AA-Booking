// app/(staff)/cases/_parts/QueryError.messages.ts
// AA-1128 (B5): the English and Vietnamese text of QueryError.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Could not load {0}.": (...a: unknown[]) => `Could not load ${a[0]}.`,
    "You do not have access to {0}": (...a: unknown[]) => `You do not have access to ${a[0]}`,
    "Your Booking role does not include this. Ask a Booking admin if you need it.": "Your Booking role does not include this. Ask a Booking admin if you need it.",
    "That status change is not allowed from the current status. Reload the case and pick again.": "That status change is not allowed from the current status. Reload the case and pick again.",
    "The case already has that status.": "The case already has that status.",
    "Confirmed is reached by approving a sent proposal version, not by a status change.": "Confirmed is reached by approving a sent proposal version, not by a status change.",
    "A reason is required for this change.": "A reason is required for this change.",
    "That staff member is not active in Booking, so the case cannot be assigned to them.": "That staff member is not active in Booking, so the case cannot be assigned to them.",
    "This customer is blocked, so a trip case cannot be opened for them.": "This customer is blocked, so a trip case cannot be opened for them.",
    "One of the countries is not set up in Booking.": "One of the countries is not set up in Booking.",
    "This trip case no longer exists.": "This trip case no longer exists.",
  },
  {
    "Could not load {0}.": (...a: unknown[]) => `Không tải được ${a[0]}.`,
    "You do not have access to {0}": (...a: unknown[]) => `Bạn không có quyền xem ${a[0]}`,
    "Your Booking role does not include this. Ask a Booking admin if you need it.": "Vai trò Booking của bạn không gồm mục này. Hãy hỏi quản trị viên Booking nếu bạn cần.",
    "That status change is not allowed from the current status. Reload the case and pick again.": "Không được đổi sang trạng thái đó từ trạng thái hiện tại. Hãy tải lại hồ sơ và chọn lại.",
    "The case already has that status.": "Hồ sơ đã ở trạng thái đó.",
    "Confirmed is reached by approving a sent proposal version, not by a status change.": "Trạng thái Đã xác nhận đạt được khi duyệt một phiên bản đề xuất đã gửi, không phải bằng đổi trạng thái.",
    "A reason is required for this change.": "Thay đổi này cần lý do.",
    "That staff member is not active in Booking, so the case cannot be assigned to them.": "Nhân viên đó chưa hoạt động trong Booking nên không thể giao hồ sơ cho họ.",
    "This customer is blocked, so a trip case cannot be opened for them.": "Khách này đã bị chặn nên không thể mở hồ sơ chuyến đi cho họ.",
    "One of the countries is not set up in Booking.": "Một trong các quốc gia chưa được thiết lập trong Booking.",
    "This trip case no longer exists.": "Hồ sơ chuyến đi này không còn tồn tại.",
  },
  ),
);
