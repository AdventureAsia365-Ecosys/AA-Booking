// ui/booking/WaveFrame.tsx
// AA-683 — a route of a later wave (Nghiệp S236 decision 3: every spec §3 route exists now). It renders
// the Figma layout frame of the page (header, KPI labels, filter labels, column headers, tabs or form
// sections) around an EmptyState "Coming in wave N". Nothing in it looks like data: no rows, no numbers.

import { Hourglass } from "lucide-react";
import { EmptyState } from "../primitives";
import { PageFrame } from "./PageFrame";
import { Section } from "./Section";

export interface WaveFrameProps {
  wave: 2 | 3;
  title: string;
  description: string;
  /** What the page will let staff do, one sentence (shown in the empty state). */
  purpose: string;
  layout: "list" | "grid" | "detail" | "form";
  actions?: string[];
  kpis?: string[];
  filters?: string[];
  columns?: string[];
  tabs?: string[];
  sections?: string[];
  breadcrumbs?: { label: string; href?: string }[];
}

function Ghost({ label }: { label: string }) {
  return (
    <span className="inline-flex h-9 items-center rounded-md border border-dashed border-line px-3 text-[13px] text-muted2">
      {label}
    </span>
  );
}

function ComingSoon({ wave, purpose }: { wave: 2 | 3; purpose: string }) {
  return (
    <EmptyState
      icon={<Hourglass size={28} />}
      title={`Coming in wave ${wave}`}
      description={purpose}
    />
  );
}

export function WaveFrame(p: WaveFrameProps) {
  const actions = p.actions?.length ? (
    <>
      {p.actions.map((a) => (
        <span
          key={a}
          aria-disabled="true"
          className="inline-flex cursor-not-allowed items-center rounded-full border border-line px-4 py-2 text-[12px] font-semibold uppercase tracking-[0.06em] text-muted2"
        >
          {a}
        </span>
      ))}
    </>
  ) : undefined;

  return (
    <PageFrame title={p.title} description={p.description} actions={actions} breadcrumbs={p.breadcrumbs}>
      <div data-testid="wave-frame" data-wave={p.wave} className="contents" />
      {p.kpis && (
        <div className="aa-kpi-grid grid grid-cols-2 gap-4 md:grid-cols-4">
          {p.kpis.map((k) => (
            <div key={k} className="rounded-lg border border-dashed border-line bg-card p-5">
              <p className="text-[13px] text-muted">{k}</p>
              <p className="mt-2 font-display text-[28px] font-semibold leading-none text-muted2">—</p>
            </div>
          ))}
        </div>
      )}
      {p.tabs && (
        <div className="flex flex-wrap gap-5 border-b border-line" role="presentation">
          {p.tabs.map((t, i) => (
            <span
              key={t}
              className={`-mb-px border-b-2 pb-2 text-[13px] ${
                i === 0 ? "border-accent font-semibold text-accent-deep" : "border-transparent text-muted2"
              }`}
            >
              {t}
            </span>
          ))}
        </div>
      )}
      {(p.layout === "list" || p.layout === "grid") && (
        <Section padded={false}>
          {p.filters && (
            <div className="flex flex-wrap gap-2 border-b border-line2 px-5 py-4">
              {p.filters.map((f) => (
                <Ghost key={f} label={f} />
              ))}
            </div>
          )}
          {p.columns && p.layout === "list" && (
            <div className="overflow-x-auto border-b border-line2 bg-panel">
              <div className="flex min-w-max gap-6 px-5 py-2.5">
                {p.columns.map((c) => (
                  <span key={c} className="min-w-[96px] text-[11px] font-semibold uppercase tracking-[0.06em] text-muted">
                    {c}
                  </span>
                ))}
              </div>
            </div>
          )}
          <ComingSoon wave={p.wave} purpose={p.purpose} />
        </Section>
      )}
      {(p.layout === "detail" || p.layout === "form") && (
        <div className={p.layout === "detail" ? "grid gap-5 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]" : "grid gap-5"}>
          <Section title={p.sections?.[0]}>
            <ComingSoon wave={p.wave} purpose={p.purpose} />
          </Section>
          {p.sections && p.sections.length > 1 && (
            <div className="flex flex-col gap-5">
              {p.sections.slice(1).map((s) => (
                <Section key={s} title={s}>
                  <p className="text-[13px] text-muted2">Available in wave {p.wave}.</p>
                </Section>
              ))}
            </div>
          )}
        </div>
      )}
    </PageFrame>
  );
}
