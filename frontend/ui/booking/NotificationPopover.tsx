"use client";
// ui/booking/NotificationPopover.tsx
// AA-683 — the topbar bell (UI spec §2.1, §4.3; Figma OVERVIEW-1): unread dot, a popover with the
// latest 5 items, "Mark all as read" and "View all". Unread items carry a gold left border.
// Data: GET /booking/notifications?limit=5 (unread_count included), polled every 60 s. Items carry a type
// icon and bold case codes (NotificationItem, fe-dashboard lane).

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bell } from "lucide-react";
import { api } from "@/lib/api/client";
import type { StaffNotification } from "@/lib/api/types";
import { DateText } from "../core/DateText";
import { NOTIFICATION_TYPE_LABEL, notificationTarget } from "./notifications";
import { NotificationIcon, NotificationMessage } from "./NotificationItem";
import { localizeIsoDates } from "./dates";

export const NOTIFICATIONS_KEY = ["notifications", "popover"] as const;

export function NotificationPopover() {
  const router = useRouter();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  const { data, isError } = useQuery({
    queryKey: NOTIFICATIONS_KEY,
    queryFn: () => api.notifications.list({ limit: 5 }),
    refetchInterval: 60_000,
    retry: false,
  });
  const unread = data?.unread_count ?? 0;

  const markAll = useMutation({
    mutationFn: () => api.notifications.markRead("all"),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["notifications"] }),
  });
  const markOne = useMutation({
    mutationFn: (id: string) => api.notifications.markRead([id]),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["notifications"] }),
  });

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [open]);

  function openItem(n: StaffNotification) {
    if (!n.read) markOne.mutate(n.id);
    const target = notificationTarget(n);
    setOpen(false);
    if (target) router.push(target.href);
  }

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-label={unread ? `Notifications, ${unread} unread` : "Notifications"}
        aria-expanded={open}
        aria-haspopup="true"
        className="relative grid h-9 w-9 cursor-pointer place-items-center rounded-[10px] border border-line bg-card text-ink3 hover:bg-bg"
      >
        <Bell size={17} />
        {unread > 0 && (
          <span
            aria-hidden="true"
            className="absolute right-1.5 top-1.5 h-2 w-2 rounded-full bg-danger ring-2 ring-card"
          />
        )}
      </button>

      {open && (
        <div className="absolute right-0 top-full z-50 mt-2 w-[420px] max-w-[calc(100vw-24px)] overflow-hidden rounded-lg border border-line bg-card shadow-[0_12px_32px_rgba(0,0,0,0.18)]">
          <div className="flex items-center justify-between border-b border-line px-4 py-3">
            <span className="text-[13px] font-semibold text-ink">Notifications</span>
            {unread > 0 && (
              <button
                type="button"
                onClick={() => markAll.mutate()}
                disabled={markAll.isPending}
                className="cursor-pointer text-xs font-semibold text-accent hover:text-accent-deep disabled:opacity-50"
              >
                Mark all as read
              </button>
            )}
          </div>
          <div className="max-h-[380px] overflow-y-auto">
            {isError ? (
              <p className="px-4 py-6 text-center text-xs text-muted">Notifications are not available right now.</p>
            ) : !data ? (
              <p className="px-4 py-6 text-center text-xs text-muted">Loading…</p>
            ) : data.items.length === 0 ? (
              <p className="px-4 py-6 text-center text-xs text-muted">You are all caught up.</p>
            ) : (
              data.items.map((n) => (
                <button
                  key={n.id}
                  type="button"
                  onClick={() => openItem(n)}
                  className={`flex w-full cursor-pointer items-start gap-3 border-b border-line2 px-4 py-3 text-left hover:bg-bg ${
                    n.read ? "border-l-2 border-l-transparent" : "border-l-2 border-l-accent bg-accent-tint/60"
                  }`}
                >
                  <NotificationIcon type={n.type} read={n.read} size={36} />
                  <span className="min-w-0 flex-1">
                    <span className={`block text-[13px] text-ink ${n.read ? "font-normal" : "font-semibold"}`}>
                      {n.title ? localizeIsoDates(n.title) : NOTIFICATION_TYPE_LABEL[n.type] || n.type}
                    </span>
                    {n.message && (
                      <NotificationMessage text={n.message} className="mt-0.5 block text-xs text-body line-clamp-2" />
                    )}
                    <span className={`mt-1 block text-[11px] ${n.read ? "text-muted2" : "text-accent-deep"}`}>
                      <DateText value={n.created_at} relative />
                    </span>
                  </span>
                </button>
              ))
            )}
          </div>
          <Link
            href="/notifications"
            onClick={() => setOpen(false)}
            className="block border-t border-line px-4 py-2.5 text-center text-xs font-semibold text-accent hover:bg-bg"
          >
            View all notifications
          </Link>
        </div>
      )}
    </div>
  );
}
