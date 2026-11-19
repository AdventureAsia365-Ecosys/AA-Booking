// app/(staff)/layout.messages.ts
// AA-1128 (B5): the English and Vietnamese text of the staff layout's full-page notices (layout.tsx, SessionNotice.tsx),
// translated on the server from the `bk_lang` cookie (the shell's provider is not around these screens).
import { defineMessages } from "@/ui/core/i18n";

export const MSG = defineMessages(
  {
    inactiveTitle: "Your Booking access is not active",
    inactiveMessage:
      "You are signed in, but your Booking profile is not active yet or was suspended. Ask a Booking admin to activate it on the Staff page.",
    noRoleTitle: "Your account has no Booking role",
    noRoleMessage:
      "You signed in with your company e-mail, but no Booking role is set for you. Ask an owner to give you a Booking role in CIS → Platform → Staff, then sign in again.",
    noRoleLink: "Open CIS → Platform → Staff",
    forbiddenTitle: "This account cannot use Booking",
    forbiddenMessage: (code: string) => `Booking refused this account (${code}). Ask a Booking admin to check your profile.`,
    downTitle: "The Booking service is not answering",
    downNoEndpoints:
      "The Booking API that this app points to does not have the staff endpoints yet (HTTP 404). They ship with the bk-003 / bk-005 backend.",
    downMessage: (status: string) => `The Booking API did not answer${status}. Try again in a moment.`,
    tryAgain: "Try again",
    signOut: "Sign out",
  },
  {
    inactiveTitle: "Quyền truy cập Booking của bạn chưa hoạt động",
    inactiveMessage:
      "Bạn đã đăng nhập, nhưng hồ sơ Booking của bạn chưa hoạt động hoặc đã bị tạm ngưng. Hãy nhờ quản trị viên Booking kích hoạt trên trang Nhân viên.",
    noRoleTitle: "Tài khoản của bạn chưa có vai trò Booking",
    noRoleMessage:
      "Bạn đã đăng nhập bằng e-mail công ty, nhưng chưa được gán vai trò Booking. Hãy nhờ chủ sở hữu gán vai trò Booking trong CIS → Platform → Staff, rồi đăng nhập lại.",
    noRoleLink: "Mở CIS → Platform → Staff",
    forbiddenTitle: "Tài khoản này không dùng được Booking",
    forbiddenMessage: (code: string) => `Booking đã từ chối tài khoản này (${code}). Hãy nhờ quản trị viên Booking kiểm tra hồ sơ của bạn.`,
    downTitle: "Dịch vụ Booking không phản hồi",
    downNoEndpoints:
      "Booking API mà ứng dụng này trỏ tới chưa có các endpoint cho nhân viên (HTTP 404). Chúng đi kèm backend bk-003 / bk-005.",
    downMessage: (status: string) => `Booking API không phản hồi${status}. Hãy thử lại sau giây lát.`,
    tryAgain: "Thử lại",
    signOut: "Đăng xuất",
  },
);
