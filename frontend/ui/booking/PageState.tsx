"use client";
// ui/booking/PageState.tsx
// AA-683 — whole-page states for detail pages (UI spec §4 "Loading / empty / error"): a skeleton that has
// the page's shape, and one error view that tells "no access" (403), "not found" (404) and "service down"
// apart instead of one generic message.

import { Lock, SearchX } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import { EmptyState, ErrorState, SkeletonBar } from "../primitives";

export function PageSkeleton({ side = true }: { side?: boolean }) {
  return (
    <main className="aa-admin-main flex-1 overflow-y-auto px-8 py-6" aria-busy="true" aria-label="Loading">
      <div className="mx-auto flex max-w-[1440px] flex-col gap-5">
        <SkeletonBar w="40%" h={26} />
        <SkeletonBar w="25%" h={14} />
        <div className={`grid gap-5 ${side ? "lg:grid-cols-[minmax(0,2fr)_minmax(320px,1fr)]" : ""}`}>
          <div className="flex flex-col gap-3 rounded-lg border border-line bg-card p-5">
            {[0, 1, 2, 3].map((i) => (
              <SkeletonBar key={i} h={64} />
            ))}
          </div>
          {side && (
            <div className="flex flex-col gap-3 rounded-lg border border-line bg-card p-5">
              <SkeletonBar w="50%" h={16} />
              <SkeletonBar h={120} />
            </div>
          )}
        </div>
      </div>
    </main>
  );
}

export function PageError({ error, onRetry, what = "this page" }: { error: unknown; onRetry?: () => void; what?: string }) {
  const status = error instanceof ApiError ? error.status : 0;
  return (
    <main className="aa-admin-main flex-1 overflow-y-auto px-8 py-6">
      <div className="mx-auto max-w-[880px] rounded-lg border border-line bg-card p-5">
        {status === 403 ? (
          <EmptyState
            icon={<Lock size={28} />}
            title="You do not have access"
            description={`Your Booking role cannot open ${what}. Ask a Booking admin if you need it.`}
          />
        ) : status === 404 ? (
          <EmptyState icon={<SearchX size={28} />} title="Not found" description={`We could not find ${what}. It may have been deleted, or the link is wrong.`} />
        ) : (
          <ErrorState message={(error as Error | null)?.message ?? `Could not load ${what}.`} onRetry={onRetry} />
        )}
      </div>
    </main>
  );
}
