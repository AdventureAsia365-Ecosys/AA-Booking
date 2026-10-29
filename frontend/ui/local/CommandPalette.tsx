"use client";
// ui/local/CommandPalette.tsx (AA-994 S4b: moved from ui/, the Booking shell; not part of the kit core)
// AA-683 — adapted copy of AA-CIS-App frontend/app/_kit/CommandPalette.tsx (CIS commit 039bb250): the ⌘K /
// Ctrl K palette. Same dialog, keyboard handling and look. Booking sources: the role's pages (ui/nav.tsx),
// trip cases (GET /booking/trip-cases?q=, roles with trip_case.read) and customers (GET /booking/customers?q=,
// roles with customer.read). Each search is skipped for a role that cannot read it.

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { Search, Briefcase, Users, CornerDownLeft } from "lucide-react";
import { K, RADIUS, sans, serif, alpha } from "../core/tokens";
import { allEntries, type NavEntry } from "./nav";
import { useStaff } from "./StaffContext";
import { api } from "@/lib/api/client";
import { canSeePage } from "@/lib/rbac";
import { TRIP_CASE_STATUS_META } from "../booking/StatusBadge";

type Row =
  | { kind: "page"; label: string; href: string; icon: React.ReactNode }
  | { kind: "case"; label: string; sub: string; href: string }
  | { kind: "customer"; label: string; sub: string; href: string };

export function CommandPalette({ open, onClose }: { open: boolean; onClose: () => void }) {
  // Returning null when closed unmounts the dialog, so each open starts with fresh state — no
  // reset-on-open setState-in-effect needed (React-Compiler safe).
  if (!open) return null;
  return <PaletteDialog onClose={onClose} />;
}

function PaletteDialog({ onClose }: { onClose: () => void }) {
  const router = useRouter();
  const { role } = useStaff();
  const canCases = canSeePage(role, "/cases");
  const canCustomers = canSeePage(role, "/customers");
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  const [selected, setSelected] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  // Focus the input on mount (DOM call, not setState — safe).
  useEffect(() => {
    const id = window.setTimeout(() => inputRef.current?.focus(), 0);
    return () => window.clearTimeout(id);
  }, []);

  // Debounce the query (~200ms). setState runs in the timer callback, not synchronously.
  useEffect(() => {
    const id = window.setTimeout(() => setDebounced(query.trim()), 200);
    return () => window.clearTimeout(id);
  }, [query]);

  const enabled = debounced.length >= 2;
  const { data: cases } = useQuery({
    queryKey: ["command-palette", "cases", debounced],
    queryFn: () => api.tripCases.list({ q: debounced, limit: 5 }),
    enabled: enabled && canCases,
    staleTime: 30_000,
    retry: false,
  });
  const { data: customers } = useQuery({
    queryKey: ["command-palette", "customers", debounced],
    queryFn: () => api.customers.list({ q: debounced, limit: 5 }),
    enabled: enabled && canCustomers,
    staleTime: 30_000,
    retry: false,
  });

  // Build the flat, grouped row list. Pages are a client-side match over the nav entries.
  const groups = useMemo(() => {
    const q = debounced.toLowerCase();
    const pageEntries: NavEntry[] = q
      ? allEntries(role).filter((e) => e.label.toLowerCase().includes(q))
      : [];
    const pages: Row[] = pageEntries.map((e) => ({
      kind: "page" as const, label: e.label, href: e.href, icon: e.icon,
    }));
    const caseRows: Row[] = (cases?.items ?? []).map((c) => ({
      kind: "case" as const,
      label: `${c.code} · ${c.customer_name ?? "Unnamed customer"}`,
      sub: [c.trip_title ?? "Custom trip", TRIP_CASE_STATUS_META[c.status]?.label].filter(Boolean).join(" · "),
      href: `/cases/${encodeURIComponent(c.code)}`,
    }));
    const customerRows: Row[] = (customers?.items ?? []).map((c) => ({
      kind: "customer" as const,
      label: c.name ?? "Unnamed customer",
      sub: [c.code, c.email ?? c.phone].filter(Boolean).join(" · "),
      href: `/customers/${encodeURIComponent(c.code ?? c.id)}`,
    }));
    return [
      { title: "Pages", rows: pages },
      { title: "Trip cases", rows: caseRows },
      { title: "Customers", rows: customerRows },
    ].filter((g) => g.rows.length > 0);
  }, [cases, customers, debounced, role]);

  const flat = useMemo(() => groups.flatMap((g) => g.rows), [groups]);

  // Keep the selection in range when the result set changes (clamp in render, not an effect).
  const safeSelected = flat.length === 0 ? 0 : Math.min(selected, flat.length - 1);

  function activate(row: Row | undefined) {
    if (!row) return;
    onClose();
    router.push(row.href);
  }

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.preventDefault(); onClose(); return; }
      if (e.key === "ArrowDown") { e.preventDefault(); setSelected((s) => Math.min(s + 1, Math.max(0, flat.length - 1))); return; }
      if (e.key === "ArrowUp") { e.preventDefault(); setSelected((s) => Math.max(s - 1, 0)); return; }
      if (e.key === "Enter") { e.preventDefault(); activate(flat[safeSelected]); return; }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [flat, safeSelected]);

  let runningIndex = -1;

  return (
    <div
      onClick={onClose}
      role="presentation"
      style={{
        position: "fixed", inset: 0, zIndex: 11000,
        background: "rgba(31,41,51,0.45)",
        display: "flex", alignItems: "flex-start", justifyContent: "center",
        padding: "12vh 16px 16px", fontFamily: sans,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="Global search"
        style={{
          width: 560, maxWidth: "100%", maxHeight: "70vh", display: "flex", flexDirection: "column",
          background: K.card, borderRadius: RADIUS.lg, border: `1px solid ${K.line}`,
          boxShadow: "0 24px 70px rgba(0,0,0,0.3)", overflow: "hidden",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "14px 16px", borderBottom: `1px solid ${K.line}` }}>
          <Search size={18} style={{ color: K.muted2, flexShrink: 0 }} />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => { setQuery(e.target.value); setSelected(0); }}
            placeholder="Search pages, trip cases, customers…"
            aria-label="Search pages, trip cases, customers"
            style={{
              flex: 1, border: "none", outline: "none", background: "transparent",
              fontSize: 15, color: K.ink, fontFamily: sans,
            }}
          />
          <kbd style={{
            fontSize: 11, color: K.muted, border: `1px solid ${K.line}`, borderRadius: 6,
            padding: "2px 6px", background: K.bg,
          }}>Esc</kbd>
        </div>

        <div style={{ overflowY: "auto", padding: 6 }}>
          {debounced.length < 2 ? (
            <div style={{ padding: "22px 14px", fontSize: 13, color: K.muted, textAlign: "center" }}>
              Type at least 2 characters to search.
            </div>
          ) : flat.length === 0 ? (
            <div style={{ padding: "22px 14px", fontSize: 13, color: K.muted, textAlign: "center" }}>
              No results for “{debounced}”.
            </div>
          ) : (
            groups.map((g) => (
              <div key={g.title} style={{ marginBottom: 4 }}>
                <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: K.muted2, padding: "8px 10px 4px" }}>
                  {g.title}
                </div>
                {g.rows.map((row) => {
                  runningIndex += 1;
                  const idx = runningIndex;
                  const isSel = idx === safeSelected;
                  return (
                    <button
                      key={`${row.kind}-${row.href}`}
                      onClick={() => activate(row)}
                      onMouseEnter={() => setSelected(idx)}
                      style={{
                        display: "flex", alignItems: "center", gap: 10, width: "100%",
                        padding: "9px 10px", borderRadius: RADIUS.md, border: "none",
                        background: isSel ? alpha(K.accent, 12) : "transparent",
                        color: K.ink, cursor: "pointer", textAlign: "left", fontFamily: sans,
                      }}
                    >
                      <span style={{ color: K.muted2, display: "flex", flexShrink: 0 }}>
                        {row.kind === "page" ? row.icon
                          : row.kind === "case" ? <Briefcase size={16} />
                          : <Users size={16} />}
                      </span>
                      <span style={{ flex: 1, minWidth: 0 }}>
                        <span style={{ display: "block", fontSize: 13.5, fontWeight: 500, color: K.ink, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          {row.label}
                        </span>
                        {"sub" in row && row.sub && (
                          <span style={{ display: "block", fontSize: 11.5, color: K.muted, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                            {row.sub}
                          </span>
                        )}
                      </span>
                      {isSel && <CornerDownLeft size={14} style={{ color: K.muted2, flexShrink: 0 }} />}
                    </button>
                  );
                })}
              </div>
            ))
          )}
        </div>

        <div style={{ display: "flex", gap: 14, padding: "8px 14px", borderTop: `1px solid ${K.line}`, fontSize: 11, color: K.muted2 }}>
          <span style={{ fontFamily: serif }}>↑ ↓ navigate</span>
          <span>↵ open</span>
          <span>esc close</span>
        </div>
      </div>
    </div>
  );
}
