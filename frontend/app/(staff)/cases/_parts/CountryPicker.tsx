"use client";
// app/(staff)/cases/_parts/CountryPicker.tsx
// AA-683 (fe-cases) — the countries of a trip case (spec §4.6 "countries (multi, ISO)"; bk-005
// trip_case_countries). Options come from GET /booking/countries (active rows); order is kept as picked,
// the API stores it as `position`. At most 20 (service.MAX_COUNTRIES).

import { useQuery } from "@tanstack/react-query";
import { X } from "lucide-react";
import { api } from "@/lib/api/client";
import { inputCls } from "./fields";
import { IconButton, Select } from "@/ui";

export const MAX_COUNTRIES = 20;

export function useCountries() {
  return useQuery({ queryKey: ["reference", "countries"], queryFn: () => api.reference.countries(), staleTime: 30 * 60_000 });
}

export function CountryPicker({ value, onChange }: { value: string[]; onChange: (codes: string[]) => void }) {
  const countries = useCountries();
  const items = countries.data?.items ?? [];
  const name = (iso: string) => items.find((c) => c.iso2 === iso)?.name ?? iso;
  const left = items.filter((c) => !value.includes(c.iso2));
  const full = value.length >= MAX_COUNTRIES;
  return (
    <div className="flex flex-col gap-2">
      {value.length > 0 && (
        <ul className="flex flex-wrap gap-1.5" aria-label="Selected countries">
          {value.map((iso) => (
            <li
              key={iso}
              className="inline-flex items-center gap-1 rounded-full border border-accent-border bg-accent-tint py-0.5 pl-2.5 pr-1 text-[12px] font-medium text-accent-deep"
            >
              {name(iso)}
              <IconButton
                variant="plain"
                size="xs"
                round
                onClick={() => onChange(value.filter((v) => v !== iso))}
                label={`Remove ${name(iso)}`}
              >
                <X size={12} />
              </IconButton>
            </li>
          ))}
        </ul>
      )}
      <Select
        className={inputCls}
        value=""
        aria-label="Add a country"
        disabled={countries.isLoading || countries.isError || full}
        onChange={(e) => e.target.value && onChange([...value, e.target.value])}
      >
        <option value="">
          {countries.isLoading
            ? "Loading countries…"
            : countries.isError
              ? "Countries could not load"
              : full
                ? `At most ${MAX_COUNTRIES} countries`
                : items.length === 0
                  ? "No country is set up in Booking yet"
                  : "Add a country…"}
        </option>
        {left.map((c) => (
          <option key={c.iso2} value={c.iso2}>
            {c.name} ({c.iso2})
          </option>
        ))}
      </Select>
    </div>
  );
}

/** "Viet Nam, Thailand" for a list of ISO codes, falling back to the code itself. */
export function useCountryNames(codes: string[]): string {
  const countries = useCountries();
  const items = countries.data?.items ?? [];
  return codes.map((iso) => items.find((c) => c.iso2 === iso)?.name ?? iso).join(", ");
}
