// lib/api/client.ts
// AA-683 — the typed browser client. Every call goes to the BFF `/api/booking/<path>`, which adds the
// Booking JWT from the httpOnly cookie and forwards to `<BOOKING_API_URL>/booking/<path>`. The browser
// never holds a token. Errors become `ApiError` with the API's code (`detail`) and message, so
// react-query's `error` is readable and a page can branch on `error.code`.

import type {
  Country,
  CursorPage,
  AuditLogItem,
  CustomerDetail,
  CustomerPatchBody,
  CustomerPatchResult,
  CustomerSummary,
  NewCustomerBody,
  NotificationFeed,
  OffsetPage,
  SettingsResponse,
  StaffDetail,
  StaffDirectoryEntry,
  StaffMe,
  StaffPreferences,
  StaffProfile,
  TripCaseDetail,
  TripCaseListItem,
  TripCaseSummary,
  ActivityItem,
  VersionListItem,
  VersionWithItems,
} from "./types";

export const BFF_BASE = "/api/booking";

export interface FieldError {
  loc: (string | number)[];
  msg: string;
}

export class ApiError extends Error {
  status: number;
  /** The API error code (`detail` string), e.g. `staff_inactive`, `version_sent`. */
  code: string;
  /** FastAPI 422 field errors, for inline form messages. */
  fieldErrors: FieldError[];
  constructor(message: string, status: number, code = "", fieldErrors: FieldError[] = []) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.fieldErrors = fieldErrors;
  }
}

type Query = Record<string, string | number | boolean | null | undefined | (string | number)[]>;

export function buildQuery(query?: Query): string {
  if (!query) return "";
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === "") continue;
    if (Array.isArray(value)) {
      if (value.length) params.set(key, value.join(","));
    } else {
      params.set(key, String(value));
    }
  }
  const s = params.toString();
  return s ? `?${s}` : "";
}

async function parse<T>(res: Response): Promise<T> {
  if (res.ok) {
    if (res.status === 204) return undefined as T;
    return (await res.json()) as T;
  }
  let code = "";
  let message = "";
  let fieldErrors: FieldError[] = [];
  try {
    const body = (await res.json()) as { detail?: unknown; message?: unknown };
    if (typeof body.detail === "string") code = body.detail;
    if (Array.isArray(body.detail)) fieldErrors = body.detail as FieldError[];
    if (typeof body.message === "string") message = body.message;
  } catch {
    /* non-JSON error body */
  }
  if (!message) {
    message =
      res.status === 401
        ? "Your session has ended. Sign in again."
        : res.status === 403
          ? "You do not have access to this."
          : fieldErrors.length
            ? "Some fields are not valid."
            : res.status >= 500
              ? "The Booking service is not answering. Try again in a moment."
              : code || `Request failed (${res.status})`;
  }
  return Promise.reject(new ApiError(message, res.status, code, fieldErrors));
}

export async function bkGet<T>(path: string, query?: Query): Promise<T> {
  const res = await fetch(`${BFF_BASE}/${path.replace(/^\/+/, "")}${buildQuery(query)}`, {
    headers: { Accept: "application/json" },
    cache: "no-store",
  });
  return parse<T>(res);
}

export async function bkSend<T>(
  path: string,
  { method = "POST", body }: { method?: "POST" | "PUT" | "PATCH" | "DELETE"; body?: unknown } = {},
): Promise<T> {
  const res = await fetch(`${BFF_BASE}/${path.replace(/^\/+/, "")}`, {
    method,
    headers: { Accept: "application/json", ...(body !== undefined ? { "Content-Type": "application/json" } : {}) },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  return parse<T>(res);
}

const enc = encodeURIComponent;

/** One typed function per Booking endpoint the W1 pages use (backend routers on the bk-681 branch). */
export const api = {
  staff: {
    me: () => bkGet<StaffMe>("staff/me"),
    preferences: () => bkGet<StaffPreferences>("staff/me/preferences"),
    setPreferences: (notify: Record<string, boolean>) =>
      bkSend<StaffPreferences>("staff/me/preferences", { method: "PUT", body: { notify } }),
    directory: (role?: string) => bkGet<{ items: StaffDirectoryEntry[] }>("staff/directory", { role }),
    list: (active?: boolean) => bkGet<{ items: StaffProfile[] }>("staff", { active }),
    get: (cisUserId: string) => bkGet<StaffDetail>(`staff/${enc(cisUserId)}`),
    update: (cisUserId: string, body: { active?: boolean; role?: string }) =>
      bkSend<StaffProfile>(`staff/${enc(cisUserId)}`, { method: "PATCH", body }),
  },
  tripCases: {
    list: (q: {
      status?: string[];
      assignee?: string;
      q?: string;
      source?: string;
      customer_id?: string;
      created_from?: string;
      created_to?: string;
      sort?: string;
      limit?: number;
      offset?: number;
    } = {}) => bkGet<OffsetPage<TripCaseListItem>>("trip-cases", q),
    summary: () => bkGet<TripCaseSummary>("trip-cases/summary"),
    get: (ref: string) => bkGet<TripCaseDetail>(`trip-cases/${enc(ref)}`),
    activity: (ref: string, before?: string) =>
      bkGet<CursorPage<ActivityItem>>(`trip-cases/${enc(ref)}/activity`, { before }),
    create: (body: Record<string, unknown>) => bkSend<TripCaseDetail>("trip-cases", { body }),
    update: (ref: string, body: Record<string, unknown>) =>
      bkSend<TripCaseDetail>(`trip-cases/${enc(ref)}`, { method: "PATCH", body }),
    changeStatus: (ref: string, to_status: string, reason?: string) =>
      bkSend<{ status: string }>(`trip-cases/${enc(ref)}/status`, { body: { to_status, reason } }),
    assign: (ref: string, cis_user_id: string | null, role = "sales") =>
      bkSend<{ changed: boolean }>(`trip-cases/${enc(ref)}/assign`, { body: { cis_user_id, role } }),
  },
  versions: {
    list: (q: {
      q?: string;
      badge?: string;
      assignee?: string;
      updated_from?: string;
      updated_to?: string;
      sort?: string;
      limit?: number;
      offset?: number;
    } = {}) => bkGet<OffsetPage<VersionListItem>>("versions", q),
    items: (ref: string, n: number) => bkGet<VersionWithItems>(`trip-cases/${enc(ref)}/versions/${n}/items`),
  },
  customers: {
    list: (q: {
      q?: string;
      status?: string;
      merge_review?: boolean;
      source?: string;
      sort?: string;
      limit?: number;
      offset?: number;
    } = {}) => bkGet<OffsetPage<CustomerSummary>>("customers", q),
    get: (ref: string) => bkGet<CustomerDetail>(`customers/${enc(ref)}`),
    /** POST /booking/customers: a manual, unverified lead (O6). One of email / phone is required. */
    create: (body: NewCustomerBody) => bkSend<CustomerDetail>("customers", { body }),
    /** PATCH /booking/customers/{ref}: name, phone, address, email (unverified only), is_blocked (admin). */
    update: (ref: string, body: CustomerPatchBody) =>
      bkSend<CustomerPatchResult>(`customers/${enc(ref)}`, { method: "PATCH", body }),
    /** DELETE /booking/customers/{ref}: archive (soft delete, admin); 409 `customer_has_cases`. */
    archive: (ref: string) => bkSend<void>(`customers/${enc(ref)}`, { method: "DELETE" }),
  },
  notifications: {
    list: (q: { unread?: boolean; type?: string; q?: string; limit?: number; before?: string } = {}) =>
      bkGet<NotificationFeed>("notifications", q),
    markRead: (ids: string[] | "all") =>
      bkSend<{ unread_count: number }>("notifications/read", {
        body: ids === "all" ? { all: true } : { ids },
      }),
  },
  reference: {
    settings: () => bkGet<SettingsResponse>("settings"),
    countries: () => bkGet<{ items: Country[] }>("countries"),
    auditLogs: (q: { actor?: string; entity_type?: string; entity_id?: string; limit?: number; before?: string } = {}) =>
      bkGet<CursorPage<AuditLogItem>>("audit-logs", q),
  },
};
