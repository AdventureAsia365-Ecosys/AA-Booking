// app/(staff)/suppliers/_parts/SupplierBankContactChanges.messages.ts
// AA-1158 part 2 (review S289): the English and Vietnamese text of SupplierBankContactChanges.tsx (kit core/i18n.ts).
// The keys are the English text; the phrasebook registers the Vietnamese side for the field labels kept in a table.
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "The supplier's contacts changed in the last {0} days. Call back on a number from the contract or earlier records.": (...a: unknown[]) =>
      `The supplier's contacts changed in the last ${a[0]} days. Call back on a number from the contract or earlier records.`,
    "contact name": "contact name",
    "contact e-mail": "contact e-mail",
    "contact phone": "contact phone",
    "contacts by role": "contacts by role",
  },
  {
    "The supplier's contacts changed in the last {0} days. Call back on a number from the contract or earlier records.": (...a: unknown[]) =>
      `Thông tin liên hệ của nhà cung cấp đã thay đổi trong ${a[0]} ngày qua. Hãy gọi lại qua số trong hợp đồng hoặc hồ sơ trước đó.`,
    "contact name": "tên người liên hệ",
    "contact e-mail": "e-mail liên hệ",
    "contact phone": "điện thoại liên hệ",
    "contacts by role": "liên hệ theo vai trò",
  },
  ),
);
