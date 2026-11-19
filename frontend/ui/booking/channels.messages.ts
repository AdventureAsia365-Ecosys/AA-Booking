// ui/booking/channels.messages.ts
// AA-1128 follow-up (S285): the Vietnamese of the pricing channel labels (lib/api/channels.ts), worded exactly as the
// channel labels of Pricing rules & FX (app/(staff)/pricing/rules/_parts/messages.ts channel_*), so the calculator,
// the proposal page and the trip plan pages show `tx(label)` in the same words. ui/booking/viOptionLabels.test.mjs
// checks both catalogues stay in step.
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
    {
      "B2C tailor-made": "B2C tailor-made",
      "Fixed departure": "Fixed departure",
      B2B: "B2B",
    },
    {
      "B2C tailor-made": "B2C thiết kế riêng",
      "Fixed departure": "Khởi hành cố định",
      B2B: "B2B",
    },
  ),
);
