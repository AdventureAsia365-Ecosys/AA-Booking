// app/(staff)/suppliers/_parts/SupplierBookingsTab.messages.ts
// AA-1128 (B5): the English and Vietnamese text of SupplierBookingsTab.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Trip case": "Trip case",
    "Booking": "Booking",
    "Service date": "Service date",
    "Net": "Net",
    "Status": "Status",
    "Ref": "Ref",
    "asked": "asked",
    "Bookings with this supplier": "Bookings with this supplier",
    "Supplier bookings of sold trips. Request, confirm or decline them on the trip case's Suppliers tab.": "Supplier bookings of sold trips. Request, confirm or decline them on the trip case's Suppliers tab.",
    "Booking status": "Booking status",
    "Live": "Live",
    "Could not load bookings": "Could not load bookings",
    "No booking here yet": "No booking here yet",
    "Bookings need the operations role": "Bookings need the operations role",
    "Bookings appear when a trip case books {0}.": (...a: unknown[]) => `Bookings appear when a trip case books ${a[0]}.`,
  },
  {
    "Trip case": "Hồ sơ chuyến đi",
    "Booking": "Booking",
    "Service date": "Ngày dịch vụ",
    "Net": "Giá net",
    "Status": "Trạng thái",
    "Ref": "Mã",
    "asked": "đã hỏi",
    "Bookings with this supplier": "Đặt chỗ với nhà cung cấp này",
    "Supplier bookings of sold trips. Request, confirm or decline them on the trip case's Suppliers tab.": "Đặt chỗ nhà cung cấp của các chuyến đã bán. Gửi yêu cầu, xác nhận hoặc từ chối ở thẻ Nhà cung cấp của hồ sơ chuyến đi.",
    "Booking status": "Trạng thái Booking",
    "Live": "Trực tiếp",
    "Could not load bookings": "Không tải được đặt chỗ",
    "No booking here yet": "Chưa có đặt chỗ ở đây",
    "Bookings need the operations role": "Đặt chỗ cần vai trò vận hành",
    "Bookings appear when a trip case books {0}.": (...a: unknown[]) => `Đặt chỗ xuất hiện khi một hồ sơ chuyến đi đặt ${a[0]}.`,
  },
  ),
);
