// ui/booking/Stepper.tsx
// AA-683 — the trip case status stepper (UI spec §4.7, Nghiệp S236 decision 4). The 12 non-cancelled DB
// statuses are grouped into 4 phases; each phase shows its own steps from `md` up and collapses to the
// phase alone on narrow screens. `cancelled` is never a step: it renders a red banner with the reason.

import { Check, XCircle } from "lucide-react";
import type { TripCaseStatus } from "@/lib/api/types";
import { TRIP_CASE_STATUS_META } from "./StatusBadge";

export const STATUS_PHASES: { key: string; label: string; statuses: TripCaseStatus[] }[] = [
  { key: "inquiry", label: "Inquiry", statuses: ["new_inquiry", "discovery"] },
  { key: "proposal", label: "Proposal", statuses: ["building", "proposal_sent"] },
  {
    key: "booking",
    label: "Booking",
    statuses: ["confirmed", "supplier_confirming", "invoice_sent", "payment_pending", "paid"],
  },
  { key: "travel", label: "Travel", statuses: ["pre_departure", "on_trip", "completed"] },
];

const ORDER: TripCaseStatus[] = STATUS_PHASES.flatMap((p) => p.statuses);

export type StepState = "done" | "current" | "future";

export function stepState(step: TripCaseStatus, current: TripCaseStatus): StepState {
  const i = ORDER.indexOf(step);
  const c = ORDER.indexOf(current);
  if (c < 0) return "future";
  if (i < c) return "done";
  if (i === c) return current === "completed" ? "done" : "current";
  return "future";
}

export function phaseOf(status: TripCaseStatus): string | null {
  return STATUS_PHASES.find((p) => p.statuses.includes(status))?.key ?? null;
}

function Dot({ state }: { state: StepState }) {
  if (state === "done") {
    return (
      <span className="grid h-5 w-5 shrink-0 place-items-center rounded-full bg-accent text-on-accent">
        <Check size={12} strokeWidth={3} />
      </span>
    );
  }
  if (state === "current") {
    return (
      <span className="grid h-5 w-5 shrink-0 place-items-center rounded-full border-2 border-accent bg-card">
        <span className="h-2 w-2 rounded-full bg-accent" />
      </span>
    );
  }
  return <span className="h-5 w-5 shrink-0 rounded-full border-2 border-line bg-card" />;
}

export function Stepper({
  status,
  cancelReason,
}: {
  status: TripCaseStatus;
  /** Shown in the cancelled banner (the last status-history note). */
  cancelReason?: string | null;
}) {
  if (status === "cancelled") {
    return (
      <div
        role="status"
        className="flex items-start gap-3 rounded-lg border border-danger-border bg-danger-soft px-4 py-3 text-danger"
      >
        <XCircle size={18} className="mt-0.5 shrink-0" />
        <div className="min-w-0">
          <p className="text-sm font-semibold">This trip case is cancelled.</p>
          {cancelReason && <p className="mt-0.5 text-[13px] text-body">Reason: {cancelReason}</p>}
        </div>
      </div>
    );
  }

  const currentPhase = phaseOf(status);
  return (
    <ol aria-label="Trip case progress" className="grid grid-cols-4 gap-2 md:gap-4">
      {STATUS_PHASES.map((phase, pi) => {
        const phaseIndex = STATUS_PHASES.findIndex((p) => p.key === currentPhase);
        const phaseState: StepState =
          pi < phaseIndex ? "done" : pi === phaseIndex ? (status === "completed" ? "done" : "current") : "future";
        return (
          <li key={phase.key} className="min-w-0">
            <div
              className={`h-1 rounded-full ${
                phaseState === "future" ? "bg-line" : phaseState === "done" ? "bg-accent" : "bg-accent-border"
              }`}
            />
            <p
              className={`mt-2 truncate text-[11px] font-semibold uppercase tracking-[0.08em] ${
                phaseState === "future" ? "text-muted2" : "text-accent-deep"
              }`}
              aria-current={phaseState === "current" ? "step" : undefined}
            >
              {phase.label}
            </p>
            {/* Phone: the phase alone, plus the current status under the current phase. */}
            {phaseState === "current" && (
              <p className="mt-0.5 truncate text-xs text-ink md:hidden">{TRIP_CASE_STATUS_META[status].label}</p>
            )}
            <ul className="mt-2 hidden flex-col gap-1.5 md:flex">
              {phase.statuses.map((s) => {
                const st = stepState(s, status);
                return (
                  <li
                    key={s}
                    className="flex min-w-0 items-center gap-2"
                    aria-current={st === "current" ? "step" : undefined}
                  >
                    <Dot state={st} />
                    <span
                      className={`truncate text-xs ${
                        st === "current" ? "font-semibold text-ink" : st === "done" ? "text-body" : "text-muted2"
                      }`}
                    >
                      {TRIP_CASE_STATUS_META[s].label}
                    </span>
                  </li>
                );
              })}
            </ul>
          </li>
        );
      })}
    </ol>
  );
}
