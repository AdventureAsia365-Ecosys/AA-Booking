// ui/local/Topbar.messages.ts
// AA-1128 (B5): the English and Vietnamese text of Topbar.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Open navigation menu": "Open navigation menu",
    "Booking": "Booking",
    "Open search": "Open search",
    "Search pages, trip cases, customers…": "Search pages, trip cases, customers…",
    "Ctrl K": "Ctrl K",
    "Search": "Search",
    "CIS console: content, tenants, staff and platform (opens in a new tab)": "CIS console: content, tenants, staff and platform (opens in a new tab)",
    "Open the CIS console (new tab)": "Open the CIS console (new tab)",
    "CIS console": "CIS console",
    "Theme: {0}. Click for {1}": (...a: unknown[]) => `Theme: ${a[0]}. Click for ${a[1]}`,
    "Theme: {0}. Switch to {1}": (...a: unknown[]) => `Theme: ${a[0]}. Switch to ${a[1]}`,
    "Account menu": "Account menu",
    "Settings": "Settings",
    "Open CIS admin": "Open CIS admin",
    "Sign out": "Sign out",
    "Light": "Light",
    "Dark": "Dark",
    "System": "System",
  },
  {
    "Open navigation menu": "Mở menu điều hướng",
    "Booking": "Booking",
    "Open search": "Mở tìm kiếm",
    "Search pages, trip cases, customers…": "Tìm trang, hồ sơ chuyến đi, khách hàng…",
    "Ctrl K": "Ctrl K",
    "Search": "Tìm kiếm",
    "CIS console: content, tenants, staff and platform (opens in a new tab)": "Bảng điều khiển CIS: nội dung, tenant, nhân viên và nền tảng (mở trong thẻ mới)",
    "Open the CIS console (new tab)": "Mở bảng điều khiển CIS (thẻ mới)",
    "CIS console": "Bảng điều khiển CIS",
    "Theme: {0}. Click for {1}": (...a: unknown[]) => `Chủ đề: ${a[0]}. Bấm để chuyển sang ${a[1]}`,
    "Theme: {0}. Switch to {1}": (...a: unknown[]) => `Chủ đề: ${a[0]}. Chuyển sang ${a[1]}`,
    "Account menu": "Menu tài khoản",
    "Settings": "Cài đặt",
    "Open CIS admin": "Mở trang quản trị CIS",
    "Sign out": "Đăng xuất",
    "Light": "Sáng",
    "Dark": "Tối",
    "System": "Hệ thống",
  },
  ),
);
