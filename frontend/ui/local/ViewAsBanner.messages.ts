// ui/local/ViewAsBanner.messages.ts — AA-1157: the owner's read-only "view as a lower role" (banner and the Roles page
// start), English and Vietnamese.
import { defineMessages } from "@/ui/core/i18n";

export const VIEW_AS_MSG = defineMessages(
  {
    bannerTitle: (label: string) => `Viewing as ${label}`,
    bannerReadOnly: "Read only: every change is refused. Your own sign-in is not changed.",
    bannerEndsIn: (minutes: number) => `Ends in ${minutes} min`,
    back: "Back",
    backing: "Going back…",
    sectionTitle: "View as a role",
    sectionIntro:
      "See Booking as a lower role sees it: its menu, pages and data, read only. The view lasts at most 60 minutes; its start, with the role's full permission list, and its end are in the audit log.",
    roleLabel: "Role",
    minutesLabel: "Length",
    minutesOption: (n: number) => `${n} minutes`,
    reasonLabel: "Reason (optional)",
    start: "Start view",
    starting: "Starting…",
    errNotDownward: "This role can see more than you can, so you cannot view as it.",
    errOwner: "Only an owner (CIS → Platform → Staff) can view as another role.",
    errUnknown: "This role cannot be viewed (unknown or archived).",
    startFailed: "The view could not be started",
  },
  {
    bannerTitle: (label: string) => `Đang xem với vai trò ${label}`,
    bannerReadOnly: "Chỉ xem: mọi thay đổi đều bị từ chối. Phiên đăng nhập của bạn không đổi.",
    bannerEndsIn: (minutes: number) => `Kết thúc sau ${minutes} phút`,
    back: "Quay lại",
    backing: "Đang quay lại…",
    sectionTitle: "Xem như một vai trò",
    sectionIntro:
      "Xem Booking như một vai trò thấp hơn: menu, trang và dữ liệu của vai trò đó, chỉ xem. Phiên xem tối đa 60 phút; lúc bắt đầu (kèm đầy đủ quyền của vai trò) và lúc kết thúc đều nằm trong nhật ký kiểm toán.",
    roleLabel: "Vai trò",
    minutesLabel: "Thời lượng",
    minutesOption: (n: number) => `${n} phút`,
    reasonLabel: "Lý do (không bắt buộc)",
    start: "Bắt đầu xem",
    starting: "Đang bắt đầu…",
    errNotDownward: "Vai trò này thấy nhiều hơn bạn, nên bạn không thể xem như vai trò đó.",
    errOwner: "Chỉ chủ sở hữu (CIS → Platform → Staff) mới xem được như vai trò khác.",
    errUnknown: "Không xem được vai trò này (không tồn tại hoặc đã lưu trữ).",
    startFailed: "Không bắt đầu được phiên xem",
  },
);
