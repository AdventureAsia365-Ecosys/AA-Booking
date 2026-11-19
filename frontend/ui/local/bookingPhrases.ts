// ui/local/bookingPhrases.ts — AA-1128 (B5): registers the catalogues of the import-free helpers of ui/booking in the
// phrasebook (ui/local/phrasebook.ts). Those helpers cannot import their own `<name>.messages.ts` (their node tests load
// them with type stripping, without the "@/" alias), so the staff shell imports this module once (AdminShell.tsx).
import "../booking/agencyFilter.messages";
import "../booking/audit.messages";
import "../booking/b2cFrozen.messages";
import "../booking/caseFacts.messages";
import "../booking/deskEntry.messages";
import "../booking/earlyBooking.messages";
import "../booking/financePeriod.messages";
import "../booking/helperPhrases.messages";
import "../booking/notifications.messages";
import "../booking/tripPlans.messages";
