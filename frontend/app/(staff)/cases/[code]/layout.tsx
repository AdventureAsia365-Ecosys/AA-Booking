"use client";
// app/(staff)/cases/[code]/layout.tsx — AA-1037 (root ADR 0019 d8): the staff profile inside one trip case. Every route
// under /cases/{code} (the workspace, the proposal builder and its preview) reads its write permissions with useCan
// from this provider, so a b2c case offers no case, version or operations write while the B2C part is closed (the API
// answers 410 b2b_only to the same writes). Until the case has loaded (or when it fails to load) its channel is
// unknown and the closed build offers no such write: the pages show their own skeleton or error meanwhile.
// The case query is the one the pages run (same key), so React Query loads it once.

import { use } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api/client";
import { B2C_INTAKE_OPEN } from "@/lib/config";
import { StaffProvider, useStaff } from "@/ui";
import { casePermissions } from "@/ui/booking/b2cFrozen";

export default function CaseLayout({ children, params }: { children: React.ReactNode; params: Promise<{ code: string }> }) {
  const { code } = use(params);
  const ref = decodeURIComponent(code);
  const staff = useStaff();
  const q = useQuery({ queryKey: ["trip-case", ref], queryFn: () => api.tripCases.get(ref) });
  return (
    <StaffProvider staff={{ ...staff, permissions: casePermissions(staff.permissions, q.data?.channel, B2C_INTAKE_OPEN) }}>
      {children}
    </StaffProvider>
  );
}
