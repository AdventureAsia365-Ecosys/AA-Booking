"use client";
// app/(staff)/pricing/policy/_parts/PolicyGuide.tsx — AA-1096 l23 (tech lead S279: "each setting needs an explanation
// or definition so I know when to change it"): the operator's guide of a pricing policy parameter on Pricing > Policy.
//
// - GuideSummary: the short "what it controls" (two lines) under the label.
// - GuideDetails: what it controls / if you raise it / if you lower it (a yes/no value: switch on / off; a day: move
//   later / earlier; a list or map: add more or bigger / remove some or smaller) / when to change it / example, then
//   the parameter's one-line help as a collapsed "Engineering note". The change drawer shows it above the inputs.
// - ExplainToggle: the per-row Explain button that opens the guide in place (in the cell's flow, never a popover over
//   the scrolling table).
// The guide comes from the API in English and Vietnamese (backend app/pricing/policy_guides.py); kit only.

import { ChevronDown, ChevronRight, Info } from "lucide-react";
import { useState } from "react";
import type { PolicyParam } from "@/lib/api/policyParams";
import { directionWords, guideFor, GUIDE_PARTS, type GuidePart } from "@/lib/policyGuide";
import { LinkButton, useLang, useT } from "@/ui";
import { M, type MessageKey } from "./messages";

type GuideParam = Pick<PolicyParam, "key" | "type" | "help" | "guide">;

function partKey(part: GuidePart, p: GuideParam): MessageKey {
  if (part === "raise") return `guideRaise_${directionWords(p.type)}`;
  if (part === "lower") return `guideLower_${directionWords(p.type)}`;
  return part === "what" ? "guideWhat" : part === "when" ? "guideWhen" : "guideExample";
}

/** The short "what it controls" under a label; the parameter's help when it has no guide. */
export function GuideSummary({ param }: { param: GuideParam }) {
  const text = guideFor(param, useLang())?.what ?? param.help;
  return (
    <span className="line-clamp-2 max-w-[360px] break-words text-xs text-muted" title={text} data-testid={`policy-what-${param.key}`}>
      {text}
    </span>
  );
}

function EngineeringNote({ text }: { text: string }) {
  const t = useT(M);
  const [open, setOpen] = useState(false);
  return (
    <div className="flex flex-col gap-1">
      <LinkButton tone="muted" size="sm" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        {open ? <ChevronDown size={12} aria-hidden /> : <ChevronRight size={12} aria-hidden />}
        {open ? t("hideNote") : t("showNote")}
      </LinkButton>
      {open && (
        <p className="border-l-2 border-line pl-2 text-xs text-muted">
          <strong>{t("engineeringNote")}:</strong> {text}
        </p>
      )}
    </div>
  );
}

/** The whole guide of one parameter, then its help as the engineering note (collapsed). */
export function GuideDetails({ param, testId }: { param: GuideParam; testId?: string }) {
  const t = useT(M);
  const guide = guideFor(param, useLang());
  return (
    <div className="flex min-w-0 flex-col gap-2 rounded-lg border border-line bg-panel px-3 py-2" data-testid={testId}>
      {guide ? (
        <dl className="m-0 flex flex-col gap-1.5">
          {GUIDE_PARTS.map((part) => (
            <div key={part} className="flex flex-col">
              <dt className="text-xs font-semibold text-ink">{t(partKey(part, param)) as string}</dt>
              <dd className="m-0 break-words text-xs text-body">{guide[part]}</dd>
            </div>
          ))}
        </dl>
      ) : (
        <p className="text-xs text-muted">{t("noGuide")}</p>
      )}
      <EngineeringNote text={param.help} />
    </div>
  );
}

/** The Explain toggle of a row and, open, the guide below it. */
export function ExplainToggle({ param }: { param: GuideParam }) {
  const t = useT(M);
  const [open, setOpen] = useState(false);
  return (
    <span className="flex max-w-[420px] flex-col items-start gap-1.5" onClick={(e) => e.stopPropagation()}>
      <span data-testid={`policy-explain-${param.key}`}>
        <LinkButton size="sm" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
          <Info size={12} aria-hidden /> {open ? t("hideExplain") : t("explain")}
        </LinkButton>
      </span>
      {open && <GuideDetails param={param} testId={`policy-guide-${param.key}`} />}
    </span>
  );
}
