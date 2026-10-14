"use client";
// app/(staff)/cases/_parts/QueryError.tsx
// AA-683 (fe-cases) — how a failed read shows on the trip case pages (spec §4 "Loading / empty / error"):
// 403 → a permission-denied empty state (the API enforces the role matrix, the page only explains it),
// anything else → the kit ErrorState with Retry.

import { ShieldOff } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import { EmptyState, ErrorState } from "@/ui";

export function isForbidden(error: unknown): boolean {
  return error instanceof ApiError && error.status === 403;
}

export function errorMessage(error: unknown, fallback = "Something went wrong."): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

/** Plain-language text for the API error codes the trip case writes return (app/trip_cases/service.py). */
const FRIENDLY: Record<string, string> = {
  illegal_transition: "That status change is not allowed from the current status. Reload the case and pick again.",
  status_unchanged: "The case already has that status.",
  approve_version_first: "Confirmed is reached by approving a sent proposal version, not by a status change.",
  reason_required: "A reason is required for this change.",
  staff_not_assignable: "That staff member is not active in Booking, so the case cannot be assigned to them.",
  customer_blocked: "This customer is blocked, so a trip case cannot be opened for them.",
  unknown_country: "One of the countries is not set up in Booking.",
  trip_case_not_found: "This trip case no longer exists.",
};

export function friendlyError(error: unknown, fallback = "Something went wrong."): string {
  if (error instanceof ApiError && FRIENDLY[error.code]) return FRIENDLY[error.code];
  return errorMessage(error, fallback);
}

export function QueryError({ error, onRetry, what }: { error: unknown; onRetry?: () => void; what: string }) {
  if (isForbidden(error)) {
    return (
      <EmptyState
        icon={<ShieldOff size={30} />}
        title={`You do not have access to ${what}`}
        description="Your Booking role does not include this. Ask a Booking admin if you need it."
      />
    );
  }
  return <ErrorState message={errorMessage(error, `Could not load ${what}.`)} onRetry={onRetry} />;
}
