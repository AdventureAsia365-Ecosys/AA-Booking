"use client";
// app/(staff)/suppliers/_parts/EditSupplierView.tsx — loads the supplier, then mounts the form keyed on its id and
// last update, so the form state starts from the saved record (no state copied in an effect).

import { useQuery } from "@tanstack/react-query";
import { suppliersApi } from "@/lib/api/suppliers";
import { PageFrame } from "@/ui";
import { DetailSkeleton } from "@/app/(staff)/_parts/DetailHeader";
import { QueryError } from "@/app/(staff)/cases/_parts/QueryError";
import { SupplierForm } from "./SupplierForm";

export function EditSupplierView({ refParam }: { refParam: string }) {
  const ref = decodeURIComponent(refParam);
  const q = useQuery({ queryKey: ["supplier", ref], queryFn: () => suppliersApi.get(ref) });
  if (q.isLoading) return <DetailSkeleton label="Loading supplier" />;
  if (q.isError || !q.data) {
    return (
      <PageFrame width="full">
        <QueryError error={q.error} onRetry={() => q.refetch()} what="this supplier" />
      </PageFrame>
    );
  }
  return <SupplierForm key={`${q.data.id}:${q.data.updated_at}`} supplier={q.data} />;
}
