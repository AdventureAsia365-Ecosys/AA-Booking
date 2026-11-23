// lib/useSelfApproval.ts — AA-1158: whether the signed-in staff member is the business owner and which self-approvals
// the policy switches allow now (GET /booking/staff/me/self-approval). Display only: the pages show the self-approval
// paths; every write checks it again server-side. Fails closed (no answer = no self-approval).
"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api/client";

export function useSelfApproval(): { owner: boolean; approve: boolean; payout: boolean } {
  const q = useQuery({ queryKey: ["staff", "self-approval"], queryFn: api.staff.selfApproval, staleTime: 60_000, retry: false });
  return { owner: !!q.data?.is_business_owner, approve: !!q.data?.approve, payout: !!q.data?.payout };
}
