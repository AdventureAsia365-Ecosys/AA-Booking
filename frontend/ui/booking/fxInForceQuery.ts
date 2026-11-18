// ui/booking/fxInForceQuery.ts
// AA-1128: the currencies with an approved FX rate in force today, read once per page (react-query, 5 minutes).
// null while loading or when the FX list cannot be read (no pricing.read): callers then keep every option.

import { useQuery } from "@tanstack/react-query";
import { pricingPolicyApi } from "@/lib/api/pricingPolicy";
import { currenciesInForce } from "./fxInForce";

export function useCurrenciesInForce(): string[] | null {
  const q = useQuery({
    queryKey: ["pricing", "fx-rates", "in-force"],
    queryFn: () => pricingPolicyApi.fxRates(),
    staleTime: 5 * 60_000,
    retry: false,
  });
  return q.data ? currenciesInForce(q.data.items) : null;
}
