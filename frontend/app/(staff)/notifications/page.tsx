"use client";
// app/(staff)/notifications/page.tsx — All notifications (W1, UI spec §4.3; Figma NOTIFICATION).
// GET /booking/notifications (keyset pages via `before`; filters unread + type + q kept in the URL),
// POST /booking/notifications/read ({ids} or {all:true}). Read state is per staff member
// (booking.notification_reads). Each item: type icon, title, message with bold case codes, timestamp, one
// action derived from its entity ("Open case") and "Mark as read".
// Not built: the Figma date-range filter — the API has no date filter and keyset pages carry no total, so the
// footer shows "N loaded" + "Show older" instead of "1 of 16 pages" (noted in AA-683 implementation notes).

import Link from "next/link";
import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { BellOff, CheckCheck, Lock } from "lucide-react";
import { ApiError, api } from "@/lib/api/client";
import type { StaffNotification } from "@/lib/api/types";
import {
  Button,
  DateText,
  EmptyState,
  ErrorState,
  FilterBar,
  NOTIFICATION_TYPE_LABEL,
  NotificationIcon,
  NotificationMessage,
  PageFrame,
  Pill,
  Section,
  SkeletonBar,
  notificationTarget,
  useToast,
  useUrlFilters,
} from "@/ui";
import { retryUnlessClientError } from "@/ui/booking/QueryProvider";

const PAGE_SIZE = 20;

export default function NotificationsPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const { get } = useUrlFilters();
  const filters = {
    unread: get("read") === "unread" ? true : get("read") === "read" ? false : undefined,
    type: get("type") || undefined,
    q: get("q") || undefined,
    limit: PAGE_SIZE,
  };
  const filtered = filters.unread !== undefined || !!filters.type || !!filters.q;
  const q = useInfiniteQuery({
    queryKey: ["notifications", "page", filters],
    queryFn: ({ pageParam }) => api.notifications.list({ ...filters, before: pageParam }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_before ?? undefined,
    retry: retryUnlessClientError,
  });
  const onDone = () => qc.invalidateQueries({ queryKey: ["notifications"] });
  const markAll = useMutation({
    mutationFn: () => api.notifications.markRead("all"),
    onSuccess: () => {
      onDone();
      toast.success("All notifications marked as read");
    },
    onError: (e) => toast.error((e as Error).message),
  });
  const markOne = useMutation({
    mutationFn: (id: string) => api.notifications.markRead([id]),
    onSuccess: onDone,
    onError: (e) => toast.error((e as Error).message),
  });
  const items = q.data?.pages.flatMap((p) => p.items) ?? [];
  const unread = q.data?.pages[0]?.unread_count ?? 0;
  const forbidden = q.error instanceof ApiError && q.error.status === 403;

  return (
    <PageFrame
      title="Notifications"
      description={
        q.data
          ? unread
            ? `${unread} unread. New inquiries, assignments, status changes and approvals that concern you.`
            : "You are all caught up. New inquiries, assignments, status changes and approvals appear here."
          : "New inquiries, assignments, status changes and approvals that concern you."
      }
      actions={
        <Button variant="secondary" onClick={() => markAll.mutate()} disabled={!unread || markAll.isPending}>
          <CheckCheck size={15} /> Mark all as read
        </Button>
      }
    >
      <Section padded={false}>
        <div className="border-b border-line2 px-4 py-4 sm:px-5">
          <FilterBar
            search={{ placeholder: "Search notifications" }}
            selects={[
              {
                key: "read",
                label: "Status",
                allLabel: "All statuses",
                options: [
                  { value: "unread", label: "Unread" },
                  { value: "read", label: "Read" },
                ],
              },
              {
                key: "type",
                label: "Type",
                allLabel: "All types",
                options: Object.entries(NOTIFICATION_TYPE_LABEL).map(([value, label]) => ({ value, label })),
              },
            ]}
          />
        </div>
        {q.isLoading ? (
          <ListSkeleton />
        ) : forbidden ? (
          <EmptyState
            icon={<Lock size={28} />}
            title="You do not have access to notifications"
            description="Booking refused this request for your role. Ask a Booking admin to check your access."
          />
        ) : q.isError ? (
          <ErrorState message={(q.error as Error).message} onRetry={() => q.refetch()} />
        ) : items.length === 0 ? (
          filtered ? (
            <EmptyState title="No notifications match these filters" description="Change or reset the filters to see more." />
          ) : (
            <EmptyState
              icon={<BellOff size={28} />}
              title="No notifications yet"
              description="When something needs you (a new inquiry, a case assigned to you, an approval) it shows up here and on the bell."
            />
          )
        ) : (
          <ul>
            {items.map((n) => (
              <NotificationRow key={n.id} n={n} onRead={(id) => markOne.mutate(id)} busy={markOne.isPending} />
            ))}
          </ul>
        )}
        {items.length > 0 && (
          <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line2 px-4 py-3 text-[13px] text-muted sm:px-5">
            <span>
              Showing {items.length} notification{items.length === 1 ? "" : "s"}
              {q.hasNextPage ? ", newest first" : ""}
            </span>
            {q.hasNextPage && (
              <Button variant="secondary" size="sm" onClick={() => q.fetchNextPage()} disabled={q.isFetchingNextPage}>
                {q.isFetchingNextPage ? "Loading…" : "Show older"}
              </Button>
            )}
          </div>
        )}
      </Section>
    </PageFrame>
  );
}

function NotificationRow({ n, onRead, busy }: { n: StaffNotification; onRead: (id: string) => void; busy: boolean }) {
  const target = notificationTarget(n);
  const label = NOTIFICATION_TYPE_LABEL[n.type] ?? n.type;
  return (
    <li
      className={`flex items-start gap-3 border-b border-line2 px-4 py-4 last:border-b-0 sm:gap-4 sm:px-5 ${
        n.read ? "border-l-2 border-l-transparent" : "border-l-2 border-l-accent bg-accent-tint/50"
      }`}
    >
      <NotificationIcon type={n.type} read={n.read} />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-1">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <span className={`text-[14px] text-ink ${n.read ? "font-medium" : "font-semibold"}`}>{n.title ?? label}</span>
            {n.title && n.title !== label && <Pill dot={false}>{label}</Pill>}
            {!n.read && <span className="sr-only">Unread</span>}
          </div>
          <span className={`shrink-0 text-xs ${n.read ? "text-muted" : "text-accent-deep"}`}>
            <DateText value={n.created_at} withTime />
          </span>
        </div>
        {n.message && <NotificationMessage text={n.message} className="mt-1 block text-[13px] text-body" />}
        {(target || !n.read) && (
          <div className="mt-3 flex flex-wrap gap-2">
            {target && (
              <Link
                href={target.href}
                onClick={() => !n.read && onRead(n.id)}
                className="inline-flex h-8 items-center rounded-full border border-accent-border px-3.5 text-xs font-semibold text-accent-deep hover:bg-accent-tint"
              >
                {target.label}
              </Link>
            )}
            {!n.read && (
              <button
                type="button"
                onClick={() => onRead(n.id)}
                disabled={busy}
                className="inline-flex h-8 cursor-pointer items-center rounded-full border border-line bg-card px-3.5 text-xs font-medium text-ink3 hover:bg-bg disabled:opacity-60"
              >
                Mark as read
              </button>
            )}
          </div>
        )}
      </div>
    </li>
  );
}

function ListSkeleton() {
  return (
    <ul aria-busy="true" aria-label="Loading notifications">
      {[0, 1, 2, 3, 4].map((i) => (
        <li key={i} className="flex items-start gap-4 border-b border-line2 px-5 py-4 last:border-b-0">
          <SkeletonBar w={40} h={40} r={20} />
          <div className="flex flex-1 flex-col gap-2">
            <SkeletonBar w="45%" h={12} />
            <SkeletonBar w="85%" h={12} />
          </div>
        </li>
      ))}
    </ul>
  );
}
