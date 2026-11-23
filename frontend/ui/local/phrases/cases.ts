// ui/local/phrases/cases.ts — AA-1128 (B5): registers the trip case, proposal, B2B desk, departure, trip check and agency billing message catalogues in the phrasebook
// (ui/local/phrasebook.ts). The staff shell imports ui/local/phrases (index.ts) once, so the server and the browser
// hold the SAME catalogues before the first render: `tx()` then gives the same text on both sides (no hydration
// mismatch), whichever pages the server rendered before. One file per page group, so the PRs of the groups never
// edit the same lines; ui/booking/phrasebookRegistry.test.mjs checks that every catalogue is listed once.
import "@/app/(staff)/agency-billing/_parts/AgencyBillingView.messages";
import "@/app/(staff)/agency-billing/_parts/BillingSettings.messages";
import "@/app/(staff)/agency-billing/_parts/EntityChanges.messages";
import "@/app/(staff)/agency-billing/_parts/TermsSection.messages";
import "@/app/(staff)/agency-billing/_parts/shared.messages";
import "@/app/(staff)/agency-billing/invoices/[ref]/page.messages";
import "@/app/(staff)/b2b-desk/_parts/AgencyBookingsTab.messages";
import "@/app/(staff)/b2b-desk/_parts/B2BDeskView.messages";
import "@/app/(staff)/b2b-desk/manifest/[id]/_parts/ManifestView.messages";
import "@/app/(staff)/cases/[code]/AgencyBookingPanel.messages";
import "@/app/(staff)/cases/[code]/AssignModal.messages";
import "@/app/(staff)/cases/[code]/ChecksTab.messages";
import "@/app/(staff)/cases/[code]/CostVsBilled.messages";
import "@/app/(staff)/cases/[code]/EditDetailsDrawer.messages";
import "@/app/(staff)/cases/[code]/NotesCard.messages";
import "@/app/(staff)/cases/[code]/ProposalsTab.messages";
import "@/app/(staff)/cases/[code]/StatusActions.messages";
import "@/app/(staff)/cases/[code]/SuppliersTab.messages";
import "@/app/(staff)/cases/[code]/SupplierGroups.messages";
import "@/app/(staff)/cases/[code]/page.messages";
import "@/app/(staff)/cases/[code]/proposals/[n]/DetailsDrawer.messages";
import "@/app/(staff)/cases/[code]/proposals/[n]/ItemDrawer.messages";
import "@/app/(staff)/cases/[code]/proposals/[n]/SupplyPicker.messages";
import "@/app/(staff)/cases/[code]/proposals/[n]/page.messages";
import "@/app/(staff)/cases/[code]/proposals/[n]/preview/page.messages";
import "@/app/(staff)/cases/[code]/useCaseMutations.messages";
import "@/app/(staff)/cases/_parts/CountryPicker.messages";
import "@/app/(staff)/cases/_parts/QueryError.messages";
import "@/app/(staff)/cases/_parts/StaffSelect.messages";
import "@/app/(staff)/cases/_parts/StatusMultiSelect.messages";
import "@/app/(staff)/cases/_parts/TripPlanPicker.messages";
import "@/app/(staff)/cases/_parts/fields.messages";
import "@/app/(staff)/cases/page.messages";
import "@/app/(staff)/departures/_parts/ChargeFeeModal.messages";
import "@/app/(staff)/departures/_parts/DepartureBoardDrawer.messages";
import "@/app/(staff)/departures/_parts/DepartureBoardView.messages";
import "@/app/(staff)/departures/_parts/shared.messages";
import "@/app/(staff)/proposals/page.messages";
import "@/app/(staff)/trip-checks/_parts/CheckDrawer.messages";
import "@/app/(staff)/trip-checks/page.messages";
