// app/(staff)/catalog/trip-plans/_parts/OrphanedPlans.messages.ts
// AA-1128 (B5): the English and Vietnamese text of OrphanedPlans.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "CIS tour key": "CIS tour key",
    "Catalog": "Catalog",
    "Since": "Since",
    "Status": "Status",
    "Still attached": "Still attached",
    "{0} open case{1} · {2} future departure{3} · {4} confirmed hold{5}": (...a: unknown[]) => `${a[0]} open case${a[1]} · ${a[2]} future departure${a[3]} · ${a[4]} confirmed hold${a[5]}`,
    "No orphaned plan with this key": "No orphaned plan with this key",
    "No orphaned trip plans": "No orphaned trip plans",
    "A trip plan shows here when CIS stops serving its tour (deleted or trashed). A paused tour is temporary and is not listed.": "A trip plan shows here when CIS stops serving its tour (deleted or trashed). A paused tour is temporary and is not listed.",
    "Agencies can no longer quote or hold these tours. Confirmed bookings, holds and trip cases are kept: cancelling is a staff decision. Search takes the exact CIS key.": "Agencies can no longer quote or hold these tours. Confirmed bookings, holds and trip cases are kept: cancelling is a staff decision. Search takes the exact CIS key.",
  },
  {
    "CIS tour key": "Mã tour CIS",
    "Catalog": "Danh mục",
    "Since": "Từ",
    "Status": "Trạng thái",
    "Still attached": "Vẫn gắn với",
    "{0} open case{1} · {2} future departure{3} · {4} confirmed hold{5}": (...a: unknown[]) => `${a[0]} hồ sơ đang mở · ${a[2]} chuyến khởi hành sắp tới · ${a[4]} giữ chỗ đã xác nhận`,
    "No orphaned plan with this key": "Không có kế hoạch mồ côi với mã này",
    "No orphaned trip plans": "Không có kế hoạch chuyến đi mồ côi",
    "A trip plan shows here when CIS stops serving its tour (deleted or trashed). A paused tour is temporary and is not listed.": "Kế hoạch chuyến đi hiện ở đây khi CIS ngừng cung cấp tour của nó (đã xoá hoặc vào thùng rác). Tour tạm dừng chỉ là tạm thời và không được liệt kê.",
    "Agencies can no longer quote or hold these tours. Confirmed bookings, holds and trip cases are kept: cancelling is a staff decision. Search takes the exact CIS key.": "Đại lý không còn báo giá hoặc giữ chỗ cho các tour này. Đặt chỗ đã xác nhận, giữ chỗ và hồ sơ chuyến đi được giữ: việc huỷ do nhân viên quyết định. Tìm kiếm dùng đúng mã CIS.",
  },
  ),
);
