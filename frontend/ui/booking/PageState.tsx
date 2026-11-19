"use client";
// ui/booking/PageState.tsx
// AA-683 — whole-page states for detail pages (UI spec §4 "Loading / empty / error"): a skeleton that has
// the page's shape, and one error view that tells "no access" (403), "not found" (404) and "service down"
// apart instead of one generic message.

import { Lock, SearchX } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import { EmptyState, ErrorState, SkeletonBar } from "../core/primitives";
import { PageFrame } from "./PageFrame";
import { useT } from "@/ui/core/useI18n";
import { MSG } from "./PageState.messages";

export function PageSkeleton({ side = true }: { side?: boolean }) {
  const t = useT(MSG);
  return (
    <PageFrame>
      <div className="flex flex-col gap-5" role="status" aria-busy="true" aria-label={t("Loading")}>
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
    </PageFrame>
  );
}

export function PageError({ error, onRetry, what = "this page" }: { error: unknown; onRetry?: () => void; what?: string }) {
  const t = useT(MSG);
  const status = error instanceof ApiError ? error.status : 0;
  return (
    <PageFrame width="narrow">
      <div className="rounded-lg border border-line bg-card p-5">
        {status === 403 ? (
          <EmptyState
            icon={<Lock size={28} />}
            title={t("You do not have access")}
            description={t("Your Booking role cannot open {0}. Ask a Booking admin if you need it.", what)}
          />
        ) : status === 404 ? (
          <EmptyState icon={<SearchX size={28} />} title={t("Not found")} description={t("We could not find {0}. It may have been deleted, or the link is wrong.", what)} />
        ) : (
          <ErrorState message={(error as Error | null)?.message ?? t("Could not load {0}.", what)} onRetry={onRetry} />
        )}
      </div>
    </PageFrame>
  );
}
