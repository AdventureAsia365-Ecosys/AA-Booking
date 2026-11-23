"use client";
// app/(staff)/roles/_parts/RolesView.tsx — Admin → Roles (AA-1155 T7; root spec R2.2, R4.4, R4.5).
// GET /booking/staff-roles (staff.admin or the CIS owner marker). Built-in roles stay in code and show read-only. A
// custom role is data: an owner (`can_edit`) clones a built-in role (New role), ticks capabilities (Edit), relabels it
// or archives / restores it; the API audits every change before and after. Approve-class capabilities are shown but
// never offered (built-in roles only; the API answers 400 if one is sent). Who holds a custom role is set on the CIS
// Staff page; its holders get the role through the booking-custom group.

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { ApiError } from "@/lib/api/client";
import { staffRolesApi, type CustomRole, type StaffRoles } from "@/lib/api/staffRoles";
import { STAFF_ROLES, roleName } from "@/lib/rbac";
import {
  Button,
  Checkbox,
  ConfirmDialog,
  DataTable,
  DateText,
  EmptyState,
  Field,
  FormDrawer,
  Input,
  PageFrame,
  Pill,
  Section,
  Select,
  useCan,
  useToast,
} from "@/ui";
import { cloneOf, groupCapabilities, sameSet, suggestCode, toggle } from "@/ui/booking/staffRoles";
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { MSG } from "./RolesView.messages";
import { ViewAsStart } from "./ViewAsStart";

function errorText(e: unknown): string {
  return e instanceof ApiError || e instanceof Error ? e.message : "Something went wrong";
}

export function RolesView() {
  const t = useT(MSG);
  const canRead = useCan("staff.admin");
  return (
    <PageFrame
      title={t("Roles")}
      description={t(
        "What each Booking role may do. Built-in roles are fixed. A custom role starts as a copy of a built-in role; an owner ticks its capabilities. Approvals stay with the built-in roles.",
      )}
    >
      {canRead ? <RolesBody /> : <EmptyState title={t("No access")} description={t("Roles are for administrators and owners.")} />}
    </PageFrame>
  );
}

type Editing = { mode: "new" } | { mode: "edit"; role: CustomRole };
type BuiltIn = StaffRoles["builtin"][number];

function RolesBody() {
  const t = useT(MSG);
  const q = useQuery({ queryKey: ["staff-roles"], queryFn: () => staffRolesApi.list() });
  const [editing, setEditing] = useState<Editing | null>(null);
  const [archiving, setArchiving] = useState<CustomRole | null>(null);
  const data = q.data;
  const canEdit = data?.can_edit === true;
  return (
    <>
      <OwnerViewAs data={data} />
      <CustomRolesSection
        roles={data?.custom ?? []}
        canEdit={canEdit}
        loading={q.isLoading}
        error={q.error ? errorText(q.error) : null}
        onNew={() => setEditing({ mode: "new" })}
        onEdit={(role) => setEditing({ mode: "edit", role })}
        onArchive={setArchiving}
      />
      <Section title={t("Built-in roles")} description={t("Fixed in Booking; a custom role can copy one.")}>
        <BuiltInTable roles={data?.builtin ?? []} loading={q.isLoading} />
      </Section>
      {editing && data ? <RoleDrawer data={data} editing={editing} onClose={() => setEditing(null)} /> : null}
      <ArchiveDialog role={archiving} onClose={() => setArchiving(null)} />
    </>
  );
}

/** AA-1157: the owner's "View as a role" panel (only when the API says this member may start one). */
function OwnerViewAs({ data }: { data: StaffRoles | undefined }) {
  return data?.can_view_as ? <ViewAsStart data={data} /> : null;
}

function CustomRolesSection(props: {
  roles: CustomRole[];
  canEdit: boolean;
  loading: boolean;
  error: string | null;
  onNew: () => void;
  onEdit: (role: CustomRole) => void;
  onArchive: (role: CustomRole) => void;
}) {
  const t = useT(MSG);
  const { canEdit } = props;
  const columns = useCustomColumns(canEdit, props.onEdit, props.onArchive);
  const description = canEdit
    ? t("You are an owner: you can create and change custom roles. Every change is in the audit log.")
    : t("Only an owner (CIS → Platform → Staff) creates or changes custom roles.");
  const newButton = (
    <Button variant="primary" onClick={props.onNew}>
      {t("New role")}
    </Button>
  );
  return (
    <Section title={t("Custom roles")} description={description} actions={canEdit ? newButton : undefined}>
      <DataTable<CustomRole>
        searchable={false}
        tableId="staff-roles-custom"
        data={props.roles}
        columns={columns}
        getRowId={(r) => r.code}
        loading={props.loading}
        error={props.error}
        emptyTitle={t("No custom roles yet")}
        emptyDescription={canEdit ? t("Start one from a built-in role with New role.") : undefined}
      />
    </Section>
  );
}

function useCustomColumns(
  canEdit: boolean,
  onEdit: (role: CustomRole) => void,
  onArchive: (role: CustomRole) => void,
): ColumnDef<CustomRole, unknown>[] {
  const t = useT(MSG);
  const tx = useTx();
  const columns: ColumnDef<CustomRole, unknown>[] = [
    {
      id: "label",
      header: t("Role"),
      cell: ({ row }) => (
        <span className="flex min-w-0 flex-col">
          <span className="truncate font-semibold text-ink">{row.original.label}</span>
          <span className="truncate font-mono text-xs text-muted">{row.original.code}</span>
        </span>
      ),
    },
    { id: "based_on", header: t("Copy of"), cell: ({ row }) => tx(roleName(row.original.based_on)) },
    {
      id: "capabilities",
      header: t("Capabilities"),
      cell: ({ row }) => (
        <span className="block max-w-[320px] truncate text-[13px] text-body" title={row.original.capabilities.join(", ")}>
          {t("{0} capabilities", row.original.capabilities.length)}
        </span>
      ),
    },
    { id: "holders", header: t("Holders"), cell: ({ row }) => row.original.holders },
    {
      id: "status",
      header: t("Status"),
      cell: ({ row }) =>
        row.original.archived ? <Pill tone="neutral">{t("Archived")}</Pill> : <Pill tone="success">{t("In use")}</Pill>,
    },
    { id: "updated", header: t("Changed"), cell: ({ row }) => <DateText className="whitespace-nowrap text-xs" value={row.original.updated_at} /> },
  ];
  if (!canEdit) return columns;
  return [
    ...columns,
    {
      id: "actions",
      header: "",
      cell: ({ row }) => (
        <span className="flex gap-2">
          <Button size="sm" variant="secondary" onClick={() => onEdit(row.original)}>
            {t("Edit")}
          </Button>
          <Button size="sm" variant="ghost" onClick={() => onArchive(row.original)}>
            {row.original.archived ? t("Restore") : t("Archive")}
          </Button>
        </span>
      ),
    },
  ];
}

function BuiltInTable({ roles, loading }: { roles: BuiltIn[]; loading: boolean }) {
  const t = useT(MSG);
  const tx = useTx();
  const columns: ColumnDef<BuiltIn, unknown>[] = [
    { id: "role", header: t("Role"), cell: ({ row }) => <span className="font-semibold text-ink">{tx(roleName(row.original.code))}</span> },
    {
      id: "capabilities",
      header: t("Capabilities"),
      cell: ({ row }) => (
        <span className="block max-w-[480px] truncate text-[13px] text-body" title={row.original.capabilities.join(", ")}>
          {row.original.capabilities.join(", ")}
        </span>
      ),
    },
  ];
  return (
    <DataTable<BuiltIn> searchable={false} tableId="staff-roles-builtin" data={roles} columns={columns} getRowId={(r) => r.code} loading={loading} />
  );
}

function ArchiveDialog({ role, onClose }: { role: CustomRole | null; onClose: () => void }) {
  const t = useT(MSG);
  const toast = useToast();
  const qc = useQueryClient();
  const restoring = role?.archived === true;
  const archive = useMutation({
    mutationFn: (r: CustomRole) => staffRolesApi.update(r.code, { archived: !r.archived }),
    onSuccess: (r) => {
      toast.success(r.archived ? t("{0} archived", r.label) : t("{0} restored", r.label));
      onClose();
      qc.invalidateQueries({ queryKey: ["staff-roles"] });
    },
    onError: (e) => toast.error(errorText(e)),
  });
  const message = restoring
    ? t("{0} can be granted again on the CIS Staff page.", role.label)
    : t("{0} holders lose their Booking access until they get another role.", role?.holders ?? 0);
  return (
    <ConfirmDialog
      open={role !== null}
      onClose={onClose}
      onConfirm={() => role && archive.mutate(role)}
      title={restoring ? t("Restore role") : t("Archive role")}
      message={message}
      confirmLabel={restoring ? t("Restore") : t("Archive")}
      destructive={!restoring}
      busy={archive.isPending}
    />
  );
}

/** The form state of the drawer: the role being made or changed, and what a save sends. */
function useRoleForm(data: StaffRoles, existing: CustomRole | null) {
  const builtIn = (code: string) => data.builtin.find((b) => b.code === code)?.capabilities ?? [];
  const [label, setLabel] = useState(existing?.label ?? "");
  const [typedCode, setTypedCode] = useState<string | null>(null);
  const [basedOn, setBasedOnState] = useState(existing?.based_on ?? "contracting");
  const [selected, setSelected] = useState<string[]>(existing?.capabilities ?? cloneOf(builtIn("contracting"), data.capabilities));
  const code = existing?.code ?? typedCode ?? suggestCode(label);
  const setBasedOn = (role: string) => {
    setBasedOnState(role);
    setSelected(cloneOf(builtIn(role), data.capabilities));
  };
  return { label, setLabel, code, setTypedCode, basedOn, setBasedOn, selected, setSelected };
}

function patchOf(existing: CustomRole, label: string, selected: string[]) {
  return {
    ...(label.trim() !== existing.label ? { label: label.trim() } : {}),
    ...(!sameSet(selected, existing.capabilities) ? { capabilities: selected } : {}),
  };
}

function isDirty(existing: CustomRole | null, label: string, selected: string[]): boolean {
  if (!existing) return label.trim() !== "" || selected.length > 0;
  return Object.keys(patchOf(existing, label, selected)).length > 0;
}

function RoleDrawer({ data, editing, onClose }: { data: StaffRoles; editing: Editing; onClose: () => void }) {
  const t = useT(MSG);
  const toast = useToast();
  const qc = useQueryClient();
  const existing = editing.mode === "edit" ? editing.role : null;
  const form = useRoleForm(data, existing);
  const save = useMutation({
    mutationFn: () =>
      existing
        ? staffRolesApi.update(existing.code, patchOf(existing, form.label, form.selected))
        : staffRolesApi.create({ code: form.code, label: form.label.trim(), based_on: form.basedOn, capabilities: form.selected }),
    onSuccess: (r) => {
      toast.success(existing ? t("{0} saved", r.label) : t("{0} created", r.label));
      qc.invalidateQueries({ queryKey: ["staff-roles"] });
      onClose();
    },
  });
  return (
    <FormDrawer
      open
      onClose={onClose}
      title={existing ? t("Edit {0}", existing.label) : t("New role")}
      submitLabel={existing ? t("Save role") : t("Create role")}
      onSubmit={() => save.mutate()}
      busy={save.isPending}
      dirty={isDirty(existing, form.label, form.selected)}
      error={save.error ? errorText(save.error) : null}
      width={560}
    >
      <Field label={t("Name")} required>
        <Input value={form.label} maxLength={80} onChange={(e) => form.setLabel(e.target.value)} />
      </Field>
      <RoleIdentityFields existing={existing} form={form} />
      <CapabilityPicker data={data} selected={form.selected} onChange={form.setSelected} busy={save.isPending} />
    </FormDrawer>
  );
}

function RoleIdentityFields({ existing, form }: { existing: CustomRole | null; form: ReturnType<typeof useRoleForm> }) {
  const t = useT(MSG);
  const tx = useTx();
  if (existing) {
    return (
      <Field label={t("Code")} hint={t("Fixed once the role exists; the CIS Staff page grants it by this code.")} locked>
        <Input value={existing.code} readOnly />
      </Field>
    );
  }
  return (
    <>
      <Field label={t("Code")} required hint={t("custom_ then lower-case letters, digits or _; the CIS Staff page grants it by this code.")}>
        <Input value={form.code} maxLength={54} onChange={(e) => form.setTypedCode(e.target.value)} />
      </Field>
      <Field label={t("Copy of")} hint={t("The capabilities start as this built-in role's, without its approvals.")}>
        <Select
          value={form.basedOn}
          onChange={(e) => form.setBasedOn(e.target.value)}
          options={STAFF_ROLES.map((r) => ({ value: r, label: tx(roleName(r)) }))}
        />
      </Field>
    </>
  );
}

function CapabilityPicker(props: { data: StaffRoles; selected: string[]; onChange: (next: string[]) => void; busy: boolean }) {
  const t = useT(MSG);
  const { data, selected } = props;
  const groups = useMemo(() => groupCapabilities(data.capabilities), [data.capabilities]);
  return (
    <Field as="div" label={t("Capabilities ({0})", selected.length)} hint={t("Approvals (locked) stay with the built-in roles.")}>
      <div className="flex flex-col gap-3">
        {groups.map((g) => (
          <fieldset key={g.area} className="flex flex-col gap-1">
            <legend className="text-xs font-semibold uppercase text-muted">{g.area}</legend>
            {g.items.map((c) => (
              <Checkbox
                key={c.name}
                label={c.assignable ? c.name : t("{0} (approval: built-in roles only)", c.name)}
                checked={selected.includes(c.name)}
                disabled={!c.assignable || props.busy}
                onChange={(e) => props.onChange(toggle(selected, c.name, e.target.checked, data.capabilities))}
              />
            ))}
          </fieldset>
        ))}
      </div>
    </Field>
  );
}
