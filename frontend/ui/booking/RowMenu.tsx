"use client";
// ui/booking/RowMenu.tsx
// AA-683 (fe-people) — the "⋯" action menu of a list row (Figma USER MANAGEMENT-4 row menu). The panel is
// `position: fixed` at the trigger's rect, so the DataTable's scrolling wrapper never clips it. Escape,
// an outside click, scrolling or picking an item closes it. Clicks never bubble to the row (onRowClick).
// AA-910: a trigger that is only partly visible is scrolled into view by the click (focus) itself; that scroll
// arrives just after opening and used to close the menu at once. Scrolls within OPEN_GRACE_MS of opening now move
// the panel to the trigger's new place instead of closing it.

import { useEffect, useRef, useState } from "react";
import { MoreHorizontal } from "lucide-react";

export interface RowMenuItem {
  label: string;
  icon?: React.ReactNode;
  onSelect: () => void;
  danger?: boolean;
  disabled?: boolean;
  /** Draw a divider above this item. */
  divider?: boolean;
}

const PANEL_WIDTH = 208;
const OPEN_GRACE_MS = 400;

/** Where the panel goes for a trigger rect: right-aligned under it, above it when there is no room below. */
function placeFor(r: DOMRect, itemCount: number): { top: number; left: number } {
  const left = Math.max(8, Math.min(r.right - PANEL_WIDTH, window.innerWidth - PANEL_WIDTH - 8));
  const below = r.bottom + 4;
  const estimated = itemCount * 38 + 12;
  const top = below + estimated > window.innerHeight ? Math.max(8, r.top - estimated - 4) : below;
  return { top, left };
}

export function RowMenu({ items, label = "Row actions" }: { items: RowMenuItem[]; label?: string }) {
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
  const btn = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const openedAt = useRef(0);
  const open = pos !== null;
  const itemCount = items.length;

  useEffect(() => {
    if (!open) return;
    const close = () => setPos(null);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        close();
        btn.current?.focus();
      }
    };
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (!panel.current?.contains(t) && !btn.current?.contains(t)) close();
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("mousedown", onDown);
    const onScroll = () => {
      const r = btn.current?.getBoundingClientRect();
      if (r && performance.now() - openedAt.current < OPEN_GRACE_MS) setPos(placeFor(r, itemCount));
      else close();
    };
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", close);
    panel.current?.querySelector<HTMLButtonElement>("button:not(:disabled)")?.focus();
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("mousedown", onDown);
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", close);
    };
  }, [open, itemCount]);

  function toggle(e: React.MouseEvent) {
    e.stopPropagation();
    if (open) return setPos(null);
    const b = btn.current;
    if (!b) return;
    // Bring a clipped trigger fully into its scroll box first, then place the panel at its final rect.
    b.scrollIntoView({ block: "nearest", inline: "nearest" });
    openedAt.current = performance.now();
    setPos(placeFor(b.getBoundingClientRect(), items.length));
  }

  if (items.length === 0) return null;
  return (
    <span onClick={(e) => e.stopPropagation()} className="inline-flex">
      <button
        ref={btn}
        type="button"
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={toggle}
        className="grid h-8 w-8 cursor-pointer place-items-center rounded-md border border-line bg-card text-muted hover:border-accent-border hover:text-ink"
      >
        <MoreHorizontal size={16} />
      </button>
      {open && (
        <div
          ref={panel}
          role="menu"
          style={{ top: pos.top, left: pos.left, width: PANEL_WIDTH }}
          className="fixed z-[9000] flex flex-col rounded-lg border border-line bg-card py-1.5 font-sans shadow-lg"
        >
          {items.map((it) => (
            <div key={it.label}>
              {it.divider && <div className="my-1 border-t border-line2" aria-hidden="true" />}
              <button
                type="button"
                role="menuitem"
                disabled={it.disabled}
                onClick={() => {
                  setPos(null);
                  it.onSelect();
                }}
                className={`flex w-full cursor-pointer items-center gap-2.5 px-3.5 py-2 text-left text-[13px] disabled:cursor-not-allowed disabled:opacity-50 ${
                  it.danger ? "text-danger hover:bg-danger-soft" : "text-body hover:bg-accent-tint"
                }`}
              >
                {it.icon && <span className="shrink-0 opacity-80">{it.icon}</span>}
                {it.label}
              </button>
            </div>
          ))}
        </div>
      )}
    </span>
  );
}
