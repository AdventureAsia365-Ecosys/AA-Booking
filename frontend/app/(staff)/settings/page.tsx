"use client";
// app/(staff)/settings/page.tsx — Settings (W1, UI spec §4.4; Figma SETTINGS, SETTINGS-1 as preferences).
// Tabs in the URL (?tab=): General | Sign-in & security | Notifications. Figma's two-column layout: a left column with
// the section title + description, a card on the right.
//   General       — profile read-only from the staff session (CIS owns name, e-mail and password: O1, so
//                   there is no password form and no photo upload), appearance (theme, per browser), and the
//                   Booking-wide commercial defaults read-only from GET /booking/settings (deposit_pct,
//                   quote_validity_days; Nghiệp S236 decision 4).
//   Sign-in & security (AA-996; replaces Figma SETTINGS-2 "change password" and RESET PASSWORD) — how this person
//                   signs in (company e-mail code through the Cognito staff sign-in; Booking holds no password, O1),
//                   the facts of the current session from GET /api/auth/session (signed in at, session end), last
//                   seen in Booking, and "Manage staff & invites in CIS ↗" (CIS → Platform → Staff; admins only,
//                   staff.admin: other roles cannot open that CIS page). No MFA claim beyond the facts: staff sign in
//                   with the e-mail code only (WORKFORCE MFA is break-glass only, ADR 0006).
//   Notifications — per-type bell toggles, GET/PUT /booking/staff/me/preferences (Figma's duplicate list tab
//                   becomes preferences, shell C-9).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Lock, LogOut, Monitor, Moon, Sun } from "lucide-react";
import { useRouter } from "next/navigation";
import { ApiError, api } from "@/lib/api/client";
import { QUOTE_VALIDITY_RANGE_DAYS } from "@/lib/bookingConfig";
import { CIS_STAFF_PATH } from "@/lib/config";
import type { SessionFacts } from "@/lib/sessionFacts";
import type { SettingsResponse, StaffPreferences } from "@/lib/api/types";
import {
  EmptyState,
  ErrorState,
  ManagedInCisLink,
  NOTIFICATION_TYPE_LABEL,
  NotificationIcon,
  PageFrame,
  SkeletonBar,
  DateText,
  staffDisplay,
  useCan,
  useStaff,
  useThemeChoice,
  useToast,
  useUrlFilters,
  type ThemeChoice,
  signOut,
} from "@/ui";
import { SavedTabs } from "@/app/(staff)/_parts/SavedTabs";
import { retryUnlessClientError } from "@/ui/booking/QueryProvider";

const TABS = [
  { key: "general", label: "General" },
  { key: "security", label: "Sign-in & security" },
  { key: "notifications", label: "Notifications" },
];

const THEMES: { key: ThemeChoice; label: string; icon: React.ReactNode }[] = [
  { key: "light", label: "Light", icon: <Sun size={15} /> },
  { key: "dark", label: "Dark", icon: <Moon size={15} /> },
  { key: "system", label: "System", icon: <Monitor size={15} /> },
];

/** What each bell type means, for the preference rows (backend STAFF_NOTIFICATION_TYPES). */
const TYPE_HELP: Record<string, string> = {
  "trip_case.new_inquiry": "A customer sent an inquiry from TripPlanner, or staff opened a new case.",
  "trip_case.assigned": "A trip case was assigned to you.",
  "trip_case.status_changed": "A case you work on moved to another status.",
  "proposal.approved": "A customer approved a proposal version.",
  // AA-996 (Figma audit): every preference row has a help line.
  "tour.retired": "A CIS tour used by an open case or a trip plan left the catalog.",
  "tour.reinstated": "A CIS tour that had left the catalog is served again.",
  "b2b.on_request": "A confirmed partner booking has lines the B2B desk must accept or decline.",
};

/** Staff-facing help of the commercial defaults (AA-996: the stored row descriptions carry internal notes). */
const SETTING_HELP: Record<string, string> = {
  deposit_pct: "Asked when the customer confirms; the balance follows the payment schedule.",
  quote_validity_days: "How long a sent proposal can be approved at its price.",
};

function isForbidden(e: unknown): boolean {
  return e instanceof ApiError && e.status === 403;
}

export default function SettingsPage() {
  const { get } = useUrlFilters();
  const tab = TABS.find((t) => t.key === get("tab"))?.key ?? "general";
  return (
    <PageFrame title="Settings" description="Your profile, how Booking looks for you, and what you are notified about.">
      <SavedTabs tabs={TABS} />
      {tab === "general" ? <GeneralTab /> : tab === "security" ? <SecurityTab /> : <NotificationsTab />}
    </PageFrame>
  );
}

/** Figma two-column settings block: title + description left (≈ 300 px), the card right; stacks on phones. */
function Block({ title, description, actions, children }: { title: string; description: string; actions?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="grid gap-4 lg:grid-cols-[300px_minmax(0,1fr)] lg:gap-8">
      <div>
        <h2 className="text-[15px] font-semibold text-ink">{title}</h2>
        <p className="mt-1 text-[13px] text-muted">{description}</p>
        {actions && <div className="mt-3">{actions}</div>}
      </div>
      <div className="min-w-0 rounded-lg border border-line bg-card p-5 sm:p-6">{children}</div>
    </section>
  );
}

function ReadOnlyField({ label, help, children }: { label: string; help?: React.ReactNode; children: React.ReactNode }) {
  // min-w-0: a grid item defaults to its min-content width, which made the one-line value push the card 40 px past a
  // 390 px phone (layout check, S254); the value truncates instead (full text in the field on wider screens).
  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <span className="text-[13px] font-medium text-ink">{label}</span>
      <span
        aria-readonly="true"
        className="flex min-h-11 items-center justify-between gap-2 rounded-md border border-line bg-panel px-3 text-[13px] text-body"
      >
        <span className="min-w-0 truncate">{children}</span>
        <Lock size={14} className="shrink-0 text-muted2" aria-label="Read-only" />
      </span>
      {help && <span className="text-xs text-muted">{help}</span>}
    </div>
  );
}

function GeneralTab() {
  const staff = useStaff();
  const { name, roleLabel } = staffDisplay(staff);
  const [theme, setTheme] = useThemeChoice();
  return (
    <div className="flex flex-col gap-8">
      <Block
        title="Profile information"
        description="You sign in with your CIS account. Name, e-mail and password are managed in CIS."
        actions={<ManagedInCisLink label="Manage in CIS" />}
      >
        <div className="grid gap-4 sm:grid-cols-2">
          {/* AA-996: a company sign-in carries no CIS role claim; the field is left out rather than shown empty. */}
          <div className={staff.cis_role ? "" : "sm:col-span-2"}>
            <ReadOnlyField label="Name">{name}</ReadOnlyField>
          </div>
          {staff.cis_role && <ReadOnlyField label="CIS role">{staff.cis_role}</ReadOnlyField>}
          <div className="sm:col-span-2">
            <ReadOnlyField label="E-mail address" help="Change your e-mail in CIS; Booking copies it at sign-in.">
              {staff.email ?? "—"}
            </ReadOnlyField>
          </div>
          <div className="sm:col-span-2">
            <ReadOnlyField label="Booking role" help="Set by a Booking admin on the Staff page.">
              {roleLabel}
            </ReadOnlyField>
          </div>
        </div>
      </Block>

      <Block title="Appearance" description="Applies to this browser only.">
        <div role="radiogroup" aria-label="Theme" className="flex flex-wrap gap-2">
          {THEMES.map((t) => (
            <button
              key={t.key}
              type="button"
              role="radio"
              aria-checked={theme === t.key}
              onClick={() => setTheme(t.key)}
              className={`inline-flex cursor-pointer items-center gap-2 rounded-full border px-4 py-2 text-[13px] font-medium ${
                theme === t.key ? "border-accent bg-accent-tint text-accent-deep" : "border-line text-muted hover:text-ink"
              }`}
            >
              {t.icon} {t.label}
            </button>
          ))}
        </div>
      </Block>

      <Block
        title="Commercial defaults"
        description="Booking-wide values every proposal and invoice uses. Read-only: the Booking team changes them, not this page."
      >
        <CommercialDefaults />
      </Block>
    </div>
  );
}

async function fetchSessionFacts(): Promise<SessionFacts> {
  const res = await fetch("/api/auth/session", { cache: "no-store" });
  if (!res.ok && res.status !== 401) throw new Error(`Session facts unavailable (HTTP ${res.status})`);
  return (await res.json()) as SessionFacts;
}

const SIGN_IN_METHOD: Record<SessionFacts["kind"], string> = {
  company_email: "Company e-mail, one-time code",
  cis_password: "CIS password (legacy bridge)",
  none: "Not signed in",
};

function epoch(value: number | null): Date | null {
  return value == null ? null : new Date(value * 1000);
}

function SecurityTab() {
  const staff = useStaff();
  // CIS → Platform → Staff is for admins; other roles would land on a CIS access-denied page.
  const canManageStaff = useCan("staff.admin");
  const router = useRouter();
  const facts = useQuery({ queryKey: ["auth", "session"], queryFn: fetchSessionFacts, staleTime: 60_000, retry: 1 });
  const f = facts.data;
  return (
    <div className="flex flex-col gap-8">
      <Block
        title="Sign-in method"
        description="Booking never stores a password. Staff sign in with their company e-mail on the Adventure Asia staff sign-in page, which sends a one-time code."
      >
        <div className="grid gap-4">
          <ReadOnlyField
            label="How you sign in"
            help={
              f?.kind === "cis_password"
                ? "This session still uses the old CIS password bridge. Sign out and choose the company e-mail sign-in."
                : "Each sign-in sends a fresh code to your company mailbox; there is no Booking password to change or reset."
            }
          >
            {facts.isLoading ? "…" : SIGN_IN_METHOD[f?.kind ?? "none"]}
          </ReadOnlyField>
          <ReadOnlyField
            label="Extra verification (MFA)"
            help="Staff sign in with a single code sent to the company mailbox; there is no app-based second factor. Booking never sees the code."
          >
            None: the one-time e-mail code is the sign-in factor
          </ReadOnlyField>
        </div>
      </Block>

      <Block
        title="This session"
        description="Your access renews by itself every few minutes until the session ends; then you sign in again."
        actions={
          <button
            type="button"
            onClick={() => void signOut(router)}
            className="inline-flex cursor-pointer items-center gap-2 rounded-full border border-line px-4 py-2 text-[13px] font-medium text-ink hover:border-accent-border"
          >
            <LogOut size={14} /> Sign out
          </button>
        }
      >
        {facts.isError ? (
          <ErrorState message={(facts.error as Error).message} onRetry={() => facts.refetch()} />
        ) : (
          <div className="grid gap-4 sm:grid-cols-2">
            <ReadOnlyField label="Signed in">{f ? <DateText value={epoch(f.signed_in_at)} withTime /> : "…"}</ReadOnlyField>
            <ReadOnlyField label="Session ends">{f ? <DateText value={epoch(f.session_ends_at)} withTime /> : "…"}</ReadOnlyField>
            <ReadOnlyField label="Last seen in Booking" help="Updated at most every 5 minutes.">
              <DateText value={staff.last_seen_at} withTime />
            </ReadOnlyField>
            <ReadOnlyField
              label="Access"
              help={staff.preview_viewer ? "Preview host: this session can read but not change anything." : undefined}
            >
              {staff.preview_viewer ? "Read-only (preview)" : "Full, by your Booking role"}
            </ReadOnlyField>
          </div>
        )}
      </Block>

      <Block
        title="Staff accounts and invites"
        description="Accounts, invitations, Booking roles and lost access are handled in CIS → Platform → Staff, by an owner or admin."
        actions={canManageStaff ? <ManagedInCisLink path={CIS_STAFF_PATH} label="Manage staff & invites in CIS" /> : undefined}
      >
        {canManageStaff ? (
          <p className="text-[13px] text-body">
            To invite a colleague, give someone a Booking role or take access away, open CIS → Platform → Staff. The person
            appears on Booking&apos;s Staff page after their first sign-in here. Lost access to your mailbox? Ask an admin to
            check your account in CIS.
          </p>
        ) : (
          <p className="text-[13px] text-body">
            Ask a Booking admin to invite a colleague, change a Booking role or restore access. The person appears on
            Booking&apos;s Staff page after their first sign-in here.
          </p>
        )}
      </Block>
    </div>
  );
}

function CommercialDefaults() {
  const q = useQuery({
    queryKey: ["settings"],
    queryFn: api.reference.settings,
    staleTime: 10 * 60_000,
    retry: retryUnlessClientError,
  });
  if (q.isLoading) {
    return (
      <div className="flex flex-col gap-4" aria-busy="true">
        <SkeletonBar h={44} />
        <SkeletonBar h={44} />
      </div>
    );
  }
  if (isForbidden(q.error)) {
    return <EmptyState icon={<Lock size={24} />} title="You do not have access to these values" />;
  }
  if (q.isError) return <ErrorState message={(q.error as Error).message} onRetry={() => q.refetch()} />;
  return <SettingsRows data={q.data!} />;
}

function SettingsRows({ data }: { data: SettingsResponse }) {
  const row = (key: string) => data.items.find((i) => i.key === key);
  const deposit = row("deposit_pct");
  const validity = row("quote_validity_days");
  const updated = (r: SettingsResponse["items"][number] | undefined) =>
    r ? (
      <>
        {SETTING_HELP[r.key] ? `${SETTING_HELP[r.key]} · ` : ""}Updated <DateText value={r.updated_at} />
      </>
    ) : (
      "Not set yet: ask the Booking team to set it."
    );
  return (
    <div className="grid gap-4">
      <ReadOnlyField label="Deposit on confirmation" help={updated(deposit)}>
        {typeof deposit?.value === "number" ? `${deposit.value}% of the approved total` : "Not set"}
      </ReadOnlyField>
      <ReadOnlyField label="Quote validity" help={updated(validity)}>
        {typeof validity?.value === "number"
          ? `${validity.value} days after sending (staff may pick ${QUOTE_VALIDITY_RANGE_DAYS.min}–${QUOTE_VALIDITY_RANGE_DAYS.max})`
          : "Not set"}
      </ReadOnlyField>
    </div>
  );
}

function NotificationsTab() {
  const toast = useToast();
  const qc = useQueryClient();
  const key = ["staff", "me", "preferences"];
  const prefs = useQuery({
    queryKey: key,
    queryFn: api.staff.preferences,
    retry: retryUnlessClientError,
  });
  const save = useMutation({
    mutationFn: (notify: Record<string, boolean>) => api.staff.setPreferences(notify),
    onMutate: async (notify) => {
      await qc.cancelQueries({ queryKey: key });
      const prev = qc.getQueryData<StaffPreferences>(key);
      qc.setQueryData<StaffPreferences>(key, (old) => ({ notify: { ...(old?.notify ?? {}), ...notify } }));
      return { prev };
    },
    onSuccess: (data) => {
      qc.setQueryData(key, data);
      toast.success("Notification preferences saved");
    },
    onError: (e, _v, ctx) => {
      if (ctx?.prev) qc.setQueryData(key, ctx.prev);
      toast.error((e as Error).message);
    },
  });
  const entries = Object.entries(prefs.data?.notify ?? {});

  return (
    <Block
      title="Bell notifications"
      description="Choose what appears on your bell and on the Notifications page. In-app only: Booking sends no e-mail yet."
    >
      {prefs.isLoading ? (
        <div className="flex flex-col gap-3" aria-busy="true">
          {[0, 1, 2, 3].map((i) => (
            <SkeletonBar key={i} h={60} r={8} />
          ))}
        </div>
      ) : isForbidden(prefs.error) ? (
        <EmptyState icon={<Lock size={24} />} title="You do not have access to preferences" />
      ) : prefs.isError ? (
        <ErrorState message={(prefs.error as Error).message} onRetry={() => prefs.refetch()} />
      ) : entries.length === 0 ? (
        <EmptyState title="No notification types yet" description="Booking has no bell notifications to configure." />
      ) : (
        <ul className="flex flex-col gap-2">
          {entries.map(([type, on]) => {
            const label = NOTIFICATION_TYPE_LABEL[type] ?? type;
            return (
              <li key={type} className="flex items-center gap-3 rounded-md bg-panel px-4 py-3">
                <NotificationIcon type={type} read={!on} size={32} />
                <div className="min-w-0 flex-1">
                  <p className="text-[13.5px] font-medium text-ink">{label}</p>
                  {TYPE_HELP[type] && <p className="mt-0.5 text-xs text-muted">{TYPE_HELP[type]}</p>}
                </div>
                <button
                  type="button"
                  role="switch"
                  aria-checked={on}
                  aria-label={label}
                  disabled={save.isPending}
                  onClick={() => save.mutate({ [type]: !on })}
                  className={`relative h-6 w-11 shrink-0 cursor-pointer rounded-full transition disabled:opacity-60 ${on ? "bg-success" : "bg-line"}`}
                >
                  <span className={`absolute top-0.5 h-5 w-5 rounded-full bg-card shadow transition ${on ? "left-[22px]" : "left-0.5"}`} />
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </Block>
  );
}
