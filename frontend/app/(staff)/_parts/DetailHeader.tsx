"use client";
// app/(staff)/_parts/DetailHeader.tsx
// AA-683 (fe-people) — the header band of a person detail page (Figma USER DETAILS / USER  DETAILS):
// a back bar "Section - Name", then initials avatar, name + badges, a meta row with icons, actions on the
// right, and the `?tab=` tabs (gold text + 2 px underline). Phone: actions wrap under the name; tabs scroll.

import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { PageFrame, Skeleton, SkeletonStyle, initialsOf, useUrlFilters } from "@/ui";

export interface DetailTab {
  key: string;
  label: string;
  count?: number;
}

export function useDetailTab(tabs: DetailTab[]): [string, (key: string) => void] {
  const { get, set } = useUrlFilters();
  const active = tabs.find((t) => t.key === get("tab"))?.key ?? tabs[0]!.key;
  return [active, (key: string) => set("tab", key === tabs[0]!.key ? null : key)];
}

export function DetailHeader({
  back,
  name,
  badges,
  meta,
  actions,
  tabs,
  activeTab,
  onTab,
}: {
  back?: { href: string; label: string };
  name: string;
  badges?: React.ReactNode;
  meta?: React.ReactNode;
  actions?: React.ReactNode;
  tabs: DetailTab[];
  activeTab: string;
  onTab: (key: string) => void;
}) {
  return (
    <div className="border-b border-line bg-bg">
      {back && (
        <div className="border-b border-line2 md:px-8">
          <div className="mx-auto flex max-w-[1440px] items-center gap-3 py-3">
            <Link
              href={back.href}
              aria-label={`Back to ${back.label.toLowerCase()}`}
              className="grid h-8 w-8 shrink-0 place-items-center rounded-md border border-line bg-card text-muted hover:text-ink"
            >
              <ArrowLeft size={16} />
            </Link>
            <p className="min-w-0 truncate text-[14px] text-muted">
              <Link href={back.href} className="hover:text-ink">
                {back.label}
              </Link>
              <span className="mx-1.5 text-muted2">/</span>
              <span className="font-medium text-ink">{name}</span>
            </p>
          </div>
        </div>
      )}
      <div className="pt-5 md:px-8 md:pt-6">
        <div className="mx-auto flex max-w-[1440px] flex-col gap-5">
          <div className="flex flex-wrap items-start gap-x-4 gap-y-3">
            <span className="grid h-14 w-14 shrink-0 place-items-center rounded-full border border-accent-border bg-accent-tint font-display text-lg font-semibold text-accent-deep">
              {initialsOf(name)}
            </span>
            <div className="min-w-0 flex-[1_1_220px]">
              <div className="flex flex-wrap items-center gap-2">
                <h1 className="min-w-0 break-words font-display text-2xl font-semibold text-ink">{name}</h1>
                {badges}
              </div>
              {meta && <div className="mt-1.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-[13px] text-muted">{meta}</div>}
            </div>
            {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
          </div>
          <div role="tablist" className="bk-tabstrip -mb-px flex gap-6">
            {tabs.map((t) => {
              const on = t.key === activeTab;
              return (
                <button
                  key={t.key}
                  role="tab"
                  aria-selected={on}
                  onClick={() => onTab(t.key)}
                  className={`shrink-0 cursor-pointer border-b-2 pb-2.5 text-[13px] ${
                    on ? "border-accent font-semibold text-accent-deep" : "border-transparent text-muted hover:text-ink"
                  }`}
                >
                  {t.label}
                  {t.count !== undefined && <span className="ml-1 text-xs text-muted2">({t.count})</span>}
                </button>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}

/** A meta-row item: icon + text (Figma: "# CUST-…", pin + location, calendar + "Added on …"). */
export function MetaItem({ icon, children }: { icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <span className="inline-flex min-w-0 items-center gap-1.5">
      <span className="shrink-0 text-muted2">{icon}</span>
      <span className="min-w-0 truncate">{children}</span>
    </span>
  );
}

/** Label-above facts in a card (Figma "Contact Information": uppercase label, value below). */
export function FactGrid({ children }: { children: React.ReactNode }) {
  return <dl className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">{children}</dl>;
}

export function Fact({ label, children, wide }: { label: string; children: React.ReactNode; wide?: boolean }) {
  return (
    <div className={`min-w-0 ${wide ? "sm:col-span-2" : ""}`}>
      <dt className="text-[11.5px] font-medium uppercase tracking-[0.06em] text-muted">{label}</dt>
      <dd className="mt-1 break-words text-[14px] text-ink">{children}</dd>
    </div>
  );
}

/** Loading state of a person detail page: the header band and two cards as shimmer bars. */
export function DetailSkeleton({ label }: { label: string }) {
  return (
    <PageFrame bleed>
      <SkeletonStyle />
      <div role="status" aria-busy="true" aria-label={label}>
      <div className="border-b border-line bg-bg py-6 md:px-8">
        <div className="mx-auto flex max-w-[1440px] items-center gap-4">
          <Skeleton w={56} h={56} r={28} />
          <div className="flex flex-1 flex-col gap-2.5">
            <Skeleton w="40%" h={22} />
            <Skeleton w="60%" h={12} />
          </div>
        </div>
      </div>
      <div className="mx-auto grid max-w-[1440px] gap-5 py-6 md:px-8 lg:grid-cols-2">
        {[0, 1].map((i) => (
          <div key={i} className="flex flex-col gap-3 rounded-lg border border-line bg-card p-5">
            <Skeleton w="35%" h={14} />
            <Skeleton h={12} />
            <Skeleton w="80%" h={12} />
            <Skeleton w="65%" h={12} />
          </div>
        ))}
      </div>
      </div>
    </PageFrame>
  );
}
