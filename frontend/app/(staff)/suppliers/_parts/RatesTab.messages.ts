// app/(staff)/suppliers/_parts/RatesTab.messages.ts
// AA-1128 (B5): the English and Vietnamese text of RatesTab.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Rate removed from the draft.": "Rate removed from the draft.",
    "No contract yet": "No contract yet",
    "Rates are entered under a contract. Add one on the Contracts tab first.": "Rates are entered under a contract. Add one on the Contracts tab first.",
    "Net rates": "Net rates",
    "This contract is a draft: add and correct its rates here. Whoever writes a rate cannot publish the contract.": "This contract is a draft: add and correct its rates here. Whoever writes a rate cannot publish the contract.",
    "Rates of a published contract are fixed. New prices are a new contract.": "Rates of a published contract are fixed. New prices are a new contract.",
    "Contract": "Contract",
    "Add rate": "Add rate",
    "No rates in this contract": "No rates in this contract",
    "Add the net price of each room type or service with Add rate.": "Add the net price of each room type or service with Add rate.",
    "This contract has no rates.": "This contract has no rates.",
    "Actions for {0}": (...a: unknown[]) => `Actions for ${a[0]}`,
    "Edit rate": "Edit rate",
    "Remove": "Remove",
    "Remove rate": "Remove rate",
    "Remove the {0} rate for": (...a: unknown[]) => `Remove the ${a[0]} rate for`,
    "from this draft?": "from this draft?",
  },
  {
    "Rate removed from the draft.": "Đã xoá giá khỏi bản nháp.",
    "No contract yet": "Chưa có hợp đồng",
    "Rates are entered under a contract. Add one on the Contracts tab first.": "Giá được nhập theo hợp đồng. Hãy thêm hợp đồng ở thẻ Hợp đồng trước.",
    "Net rates": "Giá net",
    "This contract is a draft: add and correct its rates here. Whoever writes a rate cannot publish the contract.": "Hợp đồng này là bản nháp: hãy thêm và sửa giá ở đây. Người nhập giá không thể công bố hợp đồng.",
    "Rates of a published contract are fixed. New prices are a new contract.": "Giá của hợp đồng đã công bố là cố định. Giá mới là một hợp đồng mới.",
    "Contract": "Hợp đồng",
    "Add rate": "Thêm giá",
    "No rates in this contract": "Hợp đồng này chưa có giá",
    "Add the net price of each room type or service with Add rate.": "Hãy thêm giá net của từng loại phòng hoặc dịch vụ bằng Thêm giá.",
    "This contract has no rates.": "Hợp đồng này không có giá.",
    "Actions for {0}": (...a: unknown[]) => `Thao tác cho ${a[0]}`,
    "Edit rate": "Sửa giá",
    "Remove": "Xoá",
    "Remove rate": "Xoá giá",
    "Remove the {0} rate for": (...a: unknown[]) => `Xoá giá ${a[0]} cho`,
    "from this draft?": "khỏi bản nháp này?",
  },
  ),
);
