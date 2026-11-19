// app/(staff)/cases/[code]/useCaseMutations.messages.ts
// AA-1128 (B5): the English and Vietnamese text of useCaseMutations.ts (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Trip case {0} is now {1}": (...a: unknown[]) => `Trip case ${a[0]} is now ${a[1]}`,
    "Trip case {0} assigned": (...a: unknown[]) => `Trip case ${a[0]} assigned`,
    "Trip case {0} is unassigned": (...a: unknown[]) => `Trip case ${a[0]} is unassigned`,
    "Trip case {0} updated": (...a: unknown[]) => `Trip case ${a[0]} updated`,
  },
  {
    "Trip case {0} is now {1}": (...a: unknown[]) => `Hồ sơ chuyến đi ${a[0]} giờ là ${a[1]}`,
    "Trip case {0} assigned": (...a: unknown[]) => `Đã giao hồ sơ chuyến đi ${a[0]}`,
    "Trip case {0} is unassigned": (...a: unknown[]) => `Hồ sơ chuyến đi ${a[0]} đã bỏ phân công`,
    "Trip case {0} updated": (...a: unknown[]) => `Đã cập nhật hồ sơ chuyến đi ${a[0]}`,
  },
  ),
);
