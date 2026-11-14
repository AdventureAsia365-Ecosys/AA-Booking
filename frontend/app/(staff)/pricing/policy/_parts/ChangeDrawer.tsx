"use client";
// app/(staff)/pricing/policy/_parts/ChangeDrawer.tsx — the "Change" drawer of Pricing → Policy (AA-1091; moved out of
// PolicyView.tsx in S280). New value, effective day, required reason, submit now or save a draft.
//
// S280 (AA-1096 settings UI audit): the API compares a proposal with the value in force on its effective day, so the
// drawer shows and pre-fills that value (the version in force or a scheduled one starting by then), lists the
// scheduled changes and warns when one starts later and will replace this change. The allowed range carries its unit,
// the bound against another parameter is written out with that parameter's label and value on the day, and both are
// checked before sending; every API error is shown in the page's language.

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { policyParamsApi, textToValue, valueToText, type ParamValue, type PolicyParam } from "@/lib/api/policyParams";
import { boundProblem, crossHolds, scheduledAfter, valueOn } from "@/lib/policyEdit";
import { Checkbox, DateField, Field, FormDrawer, Input, Select, Textarea, useT, useToast } from "@/ui";
import { M, type MessageKey } from "./messages";
import { GuideDetails } from "./PolicyGuide";
import { crossText, errorText, rangeText, useLabel, useShow, type T } from "./policyUi";

function todayUtc(): string {
  return new Date().toISOString().slice(0, 10);
}

const LISTS: ReadonlySet<PolicyParam["type"]> = new Set(["month_days", "codes", "integers"]);

/** The field hint: the shape of the value, then the allowed range with its unit, then the cross bound. */
function valueHint(t: T, p: PolicyParam, cross: string | null): string | undefined {
  const range = p.type === "amount_map" ? rangeText(t, { ...p, choices: [] }) : rangeText(t, p);
  const parts: string[] = [];
  if (p.type === "currency_map") parts.push(t("fieldValueMap") as string);
  if (p.type === "amount_map") parts.push(t("fieldValueAmounts", p.choices.join(", ")) as string);
  if (LISTS.has(p.type)) parts.push(t("fieldValueList") as string);
  if (range && p.type !== "currency_map") parts.push(t("fieldValueHint", range) as string);
  if (cross) parts.push(t("crossHint", cross) as string);
  return parts.length ? parts.join(" ") : undefined;
}

/** The local refusal of a typed value (shape, range, cross bound), in the page's language; null when it may be sent. */
function localProblem(t: T, p: PolicyParam, value: ParamValue | null, other: ParamValue | null | undefined,
  cross: string | null): string | null {
  if (value === null) return t("invalidValue") as string;
  const bound = boundProblem(p, value);
  if (bound === "min") return t("errMin", p.minimum ?? "") as string;
  if (bound === "max") return t("errMax", p.maximum ?? "") as string;
  if (p.cross_bound && cross && !crossHolds(p.cross_bound.relation, value, other)) return t("errCross", cross) as string;
  return null;
}

function ScheduledNotes({ param, day }: { param: PolicyParam; day: string }) {
  const t = useT(M);
  const show = useShow();
  if (!param.scheduled.length) return null;
  const later = scheduledAfter(param, day)[0];
  return (
    <div className="flex flex-col gap-1 text-[13px]" data-testid="policy-change-scheduled">
      <span className="font-medium text-ink">{t("scheduledListTitle")}</span>
      <ul className="flex flex-col gap-0.5 text-body">
        {scheduledAfter(param, "").map((v) => (
          <li key={v.id}>{t("scheduledChange", show(param, v.value), v.effective_from)}</li>
        ))}
      </ul>
      {later && <p className="text-warning">{t("scheduledLater", show(param, later.value), later.effective_from)}</p>}
    </div>
  );
}

export function ChangeDrawer({ param, items, onClose }: { param: PolicyParam; items: PolicyParam[]; onClose: () => void }) {
  const t = useT(M);
  const show = useShow();
  const label = useLabel()(param);
  const qc = useQueryClient();
  const toast = useToast();
  const [effective, setEffective] = useState(todayUtc());
  const onDay = valueOn(param, effective);
  const [text, setText] = useState(valueToText(onDay?.value, param.type));
  const [reason, setReason] = useState("");
  const [submitNow, setSubmitNow] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // A new day pre-fills the value on that day, unless the maker already typed another value.
  const changeDay = (day: string) => {
    if (text === valueToText(onDay?.value, param.type)) setText(valueToText(valueOn(param, day)?.value, param.type));
    setEffective(day);
  };
  const cross = crossText(t, show, param.cross_bound, items, effective);
  const other = param.cross_bound ? valueOn(items.find((i) => i.key === param.cross_bound?.key) ?? { current: null, scheduled: [] }, effective)?.value : null;
  const save = useMutation({
    mutationFn: () => {
      const value = textToValue(text, param.type);
      const problem = localProblem(t, param, value, other, cross);
      if (problem || value === null) throw new Error(problem ?? (t("invalidValue") as string));
      return policyParamsApi.propose({ key: param.key, value, effective_from: effective, reason, submit: submitNow });
    },
    onSuccess: (v) => {
      qc.invalidateQueries({ queryKey: ["pricing", "policy"] });
      toast.success(t("saved", t(`status_${v.status}` as MessageKey) as string) as string);
      onClose();
    },
    onError: (err) => setError(errorText(t, err, { param, step: "propose" })),
  });
  return (
    <FormDrawer
      open
      onClose={onClose}
      title={t("drawerTitle", label) as string}
      onSubmit={() => {
        setError(null);
        save.mutate();
      }}
      submitLabel={t("save") as string}
      busy={save.isPending}
      dirty={!!reason.trim()}
      error={error}
      width={520}
    >
      <div className="flex flex-col gap-4" data-testid="policy-change-drawer">
        <GuideDetails param={param} testId="policy-change-guide" />
        <p className="text-[13px] text-body" data-testid="policy-change-current">
          {t("currentIs", show(param, param.current?.value))}
          {onDay && onDay.id !== param.current?.id ? ` · ${t("valueOnDay", effective, show(param, onDay.value))}` : ""}
        </p>
        <ScheduledNotes param={param} day={effective} />
        <Field label={t("fieldValue")} required hint={valueHint(t, param, cross)}>
          {param.type === "boolean" ? (
            <Select
              value={text}
              onChange={(e) => setText(e.target.value)}
              options={[
                { value: "true", label: t("yes") },
                { value: "false", label: t("no") },
              ]}
            />
          ) : (
            <Input value={text} onChange={(e) => setText(e.target.value)} data-testid="policy-value" />
          )}
        </Field>
        <Field label={t("fieldEffective")} required hint={t("fieldEffectiveHint") as string}>
          <DateField value={effective} onChange={changeDay} min={todayUtc()} />
        </Field>
        <Field label={t("fieldReason")} required hint={t("fieldReasonHint") as string}>
          <Textarea value={reason} onChange={(e) => setReason(e.target.value)} maxLength={300} rows={3} />
        </Field>
        <Checkbox checked={submitNow} onChange={(e) => setSubmitNow(e.target.checked)} label={t("fieldSubmit")} />
      </div>
    </FormDrawer>
  );
}
