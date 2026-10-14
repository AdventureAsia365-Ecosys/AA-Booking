"use client";
// ui/booking/QueryProvider.tsx
// AA-683 — the app-wide react-query client for Booking (S236 review finding 9). Same defaults as the CIS kit
// Providers (ui/Providers.tsx, kept byte-comparable with CIS and no longer mounted), plus two Booking rules:
//   - a 401 from any query or mutation means the session is over (the BFF has already cleared both cookies):
//     go to /login?reason=expired&next=<this page> instead of showing an error whose Retry cannot succeed;
//   - 4xx answers are final, so they are not retried (a retry only delays the error state). Network errors
//     and 5xx keep one retry.

import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError } from "@/lib/api/client";

let redirecting = false;

function onSessionEnded(error: unknown) {
  if (!(error instanceof ApiError) || error.status !== 401 || redirecting || typeof window === "undefined") return;
  redirecting = true;
  const here = `${window.location.pathname}${window.location.search}`;
  window.location.assign(`/login?reason=expired&next=${encodeURIComponent(here)}`);
}

export function retryUnlessClientError(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && error.status >= 400 && error.status < 500) return false;
  return failureCount < 1;
}

export function BookingQueryProvider({ children }: { children: React.ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        queryCache: new QueryCache({ onError: onSessionEnded }),
        mutationCache: new MutationCache({ onError: onSessionEnded }),
        defaultOptions: {
          queries: {
            staleTime: 30_000,
            gcTime: 5 * 60_000,
            refetchOnWindowFocus: false,
            retry: retryUnlessClientError,
          },
          mutations: {
            retry: 0,
          },
        },
      }),
  );
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
