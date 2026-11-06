"use client";
// app/(staff)/catalog/trip-plans/_parts/UseForTripCaseModal.tsx — "Use for trip case" (UI spec §6.3, Figma
// TRIP TEMPLATE DETAILS-1; AA-679). POST /booking/catalog/trip-plans/{source_id}/trip-cases adds a NEW VERSION built
// from the plan's days to an existing case (by its TC- code). A new staff-made case for a customer or a lead was a B2C
// case, retired with the B2C half of Booking (AA-1046: the API answers 410 b2b_only). Nothing is priced: the version
// starts unpriced, and sales add cost lines in the builder. Only sellable plans.

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/lib/api/client";
import { tripPlansApi, type TripPlanDetail } from "@/lib/api/tripPlans";
import { Input, Button, Modal, fieldName, useToast } from "@/ui";
import { Field, inputCls } from "../../../cases/_parts/fields";

const CASE_CODE_RE = /^TC-\d{6,}$/i; // lpad 6, grows past TC-999999 (bk-002 trip_case_code_seq)

export function UseForTripCaseModal({ plan, onClose }: { plan: TripPlanDetail; onClose: () => void }) {
  const router = useRouter();
  const toast = useToast();
  const qc = useQueryClient();
  const [caseCode, setCaseCode] = useState("");
  const [tried, setTried] = useState(false);

  const codeInvalid = !CASE_CODE_RE.test(caseCode.trim());
  const invalid = codeInvalid;
  const dayCount = plan.days.length || 1;

  const use = useMutation({
    mutationFn: () => tripPlansApi.useForTripCase(plan.source_id, { trip_case: caseCode.trim().toUpperCase() }),
    onSuccess: (out) => {
      qc.invalidateQueries({ queryKey: ["trip-cases"] });
      qc.invalidateQueries({ queryKey: ["trip-plans", "detail", plan.source_id] });
      const code = out.trip_case.code;
      toast.success(`V${out.version.version_number} added to ${code} from this plan`);
      router.push(`/cases/${encodeURIComponent(code)}?tab=proposals`);
    },
  });

  function submit() {
    setTried(true);
    if (invalid || use.isPending) return;
    use.mutate();
  }

  const err = use.error instanceof ApiError ? use.error : null;
  const fieldErrors = err?.fieldErrors ?? [];
  return (
    <Modal
      open
      onClose={() => !use.isPending && onClose()}
      title="Use for trip case"
      width={620}
      footer={
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose} disabled={use.isPending}>
            Cancel
          </Button>
          <Button variant="primary" onClick={submit} disabled={use.isPending}>
            {use.isPending ? "Adding…" : "Add version"}
          </Button>
        </div>
      }
    >
      <form
        className="flex flex-col gap-4 font-sans"
        noValidate
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <p className="text-[13px] text-muted">
          The proposal starts with the {dayCount} {dayCount === 1 ? "item" : "day items"} of{" "}
          <span className="font-medium text-ink">{plan.title}</span>, titled from CIS. It is not priced: add cost lines and
          price it in the builder.
        </p>
          <Field
            label="Trip case code"
            required
            hint="The plan's days become a new version of that case; its earlier versions stay."
            error={tried && codeInvalid ? "Enter a case code like TC-000123." : null}
          >
            <Input className={inputCls} placeholder="TC-000123" value={caseCode} onChange={(e) => setCaseCode(e.target.value)} />
          </Field>

        {err && (
          <div role="alert" className="rounded-md border border-danger-border bg-danger-soft px-3 py-2 text-[13px] text-danger">
            {fieldErrors.length ? (
              <ul className="list-disc pl-4">
                {fieldErrors.map((f, i) => (
                  <li key={i}>
                    <span className="font-medium">{fieldName(f) || "form"}</span>: {f.msg}
                  </li>
                ))}
              </ul>
            ) : err.code === "trip_case_not_found" ? (
              <p>No open trip case has that code.</p>
            ) : err.code === "trip_case_closed" ? (
              <p>That trip case is closed (completed or cancelled). Reopen it first, or create a new case.</p>
            ) : (
              <p>{err.message}</p>
            )}
          </div>
        )}
        <button type="submit" hidden aria-hidden="true" tabIndex={-1} />
      </form>
    </Modal>
  );
}
