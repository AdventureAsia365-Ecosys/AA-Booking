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

function RolesBody() {
  const t = useT(MSG);
  const tx = useTx();
  const toast = useToast();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["staff-roles"], queryFn: () => staffRolesApi.list() });
  const [editing, setEditing] = useState<Editing | null>(null);
  const [archiving, setArchiving] = useState<CustomRole | null>(null);
  const data = q.data;
  const canEdit = data?.can_edit === true;
  const archive = useMutation({
    mutationFn: (r: CustomRole) => staffRolesApi.update(r.code, { archived: !r.archived }),
    onSuccess: (r) => {
      toast.success(r.archived ? t("{0} archived", r.label) : t("{0} restored", r.label));
      setArchiving(null);
      qc.invalidateQueries({ queryKey: ["staff-roles"] });
    },
    onError: (e) => toast.error(errorText(e)),
  });

  const customColumns: ColumnDef<CustomRole, unknown>[] = [
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
    {
      id: "actions",
      header: "",
      cell: ({ row }) =>
        canEdit ? (
          <span className="flex gap-2">
            <Button size="sm" variant="secondary" onClick={() => setEditing({ mode: "edit", role: row.original })}>
              {t("Edit")}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setArchiving(row.original)}>
              {row.original.archived ? t("Restore") : t("Archive")}
            </Button>
          </span>
        ) : null,
    },
  ];

  const builtInColumns: ColumnDef<StaffRoles["builtin"][number], unknown>[] = [
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
    <>
      <Section
        title={t("Custom roles")}
        description={
          canEdit
            ? t("You are an owner: you can create and change custom roles. Every change is in the audit log.")
            : t("Only an owner (CIS → Platform → Staff) creates or changes custom roles.")
        }
        actions={
          canEdit ? (
            <Button variant="primary" onClick={() => setEditing({ mode: "new" })}>
              {t("New role")}
            </Button>
          ) : undefined
        }
      >
        <DataTable<CustomRole>
          searchable={false}
          tableId="staff-roles-custom"
          data={data?.custom ?? []}
          columns={customColumns}
          getRowId={(r) => r.code}
          loading={q.isLoading}
          error={q.error ? errorText(q.error) : undefined}
          emptyTitle={t("No custom roles yet")}
          emptyDescription={canEdit ? t("Start one from a built-in role with New role.") : undefined}
        />
      </Section>
      <Section title={t("Built-in roles")} description={t("Fixed in Booking; a custom role can copy one.")}>
        <DataTable<StaffRoles["builtin"][number]>
          searchable={false}
          tableId="staff-roles-builtin"
          data={data?.builtin ?? []}
          columns={builtInColumns}
          getRowId={(r) => r.code}
          loading={q.isLoading}
        />
      </Section>
      {editing && data ? <RoleDrawer data={data} editing={editing} onClose={() => setEditing(null)} /> : null}
      <ConfirmDialog
        open={archiving !== null}
        onClose={() => setArchiving(null)}
        onConfirm={() => archiving && archive.mutate(archiving)}
        title={archiving?.archived ? t("Restore role") : t("Archive role")}
        message={
          archiving?.archived
            ? t("{0} can be granted again on the CIS Staff page.", archiving?.label ?? "")
            : t("{0} holders lose their Booking access until they get another role.", archiving?.holders ?? 0)
        }
        confirmLabel={archiving?.archived ? t("Restore") : t("Archive")}
        destructive={!archiving?.archived}
        busy={archive.isPending}
      />
    </>
  );
}

function RoleDrawer({ data, editing, onClose }: { data: StaffRoles; editing: Editing; onClose: () => void }) {
  const t = useT(MSG);
  const tx = useTx();
  const toast = useToast();
  const qc = useQueryClient();
  const existing = editing.mode === "edit" ? editing.role : null;
  const builtIn = (code: string) => data.builtin.find((b) => b.code === code)?.capabilities ?? [];
  const [label, setLabel] = useState(existing?.label ?? "");
  const [code, setCode] = useState(existing?.code ?? "");
  const [codeTouched, setCodeTouched] = useState(false);
  const [basedOn, setBasedOn] = useState(existing?.based_on ?? "contracting");
  const [selected, setSelected] = useState<string[]>(existing?.capabilities ?? cloneOf(builtIn("contracting"), data.capabilities));
  const groups = useMemo(() => groupCapabilities(data.capabilities), [data.capabilities]);
  const shownCode = existing ? existing.code : codeTouched ? code : suggestCode(label);
  const dirty = existing
    ? label.trim() !== existing.label || !sameSet(selected, existing.capabilities)
    : label.trim() !== "" || selected.length > 0;

  const save = useMutation({
    mutationFn: () =>
      existing
        ? staffRolesApi.update(existing.code, {
            ...(label.trim() !== existing.label ? { label: label.trim() } : {}),
            ...(!sameSet(selected, existing.capabilities) ? { capabilities: selected } : {}),
          })
        : staffRolesApi.create({ code: shownCode, label: label.trim(), based_on: basedOn, capabilities: selected }),
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
      dirty={dirty}
      error={save.error ? errorText(save.error) : null}
      width={560}
    >
      <Field label={t("Name")} required>
        <Input value={label} maxLength={80} onChange={(e) => setLabel(e.target.value)} />
      </Field>
      {existing ? (
        <Field label={t("Code")} hint={t("Fixed once the role exists; the CIS Staff page grants it by this code.")} locked>
          <Input value={existing.code} readOnly />
        </Field>
      ) : (
        <>
          <Field label={t("Code")} required hint={t("custom_ then lower-case letters, digits or _; the CIS Staff page grants it by this code.")}>
            <Input
              value={shownCode}
              maxLength={54}
              onChange={(e) => {
                setCodeTouched(true);
                setCode(e.target.value);
              }}
            />
          </Field>
          <Field label={t("Copy of")} hint={t("The capabilities start as this built-in role's, without its approvals.")}>
            <Select
              value={basedOn}
              onChange={(e) => {
                setBasedOn(e.target.value);
                setSelected(cloneOf(builtIn(e.target.value), data.capabilities));
              }}
              options={STAFF_ROLES.map((r) => ({ value: r, label: tx(roleName(r)) }))}
            />
          </Field>
        </>
      )}
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
                  disabled={!c.assignable || save.isPending}
                  onChange={(e) => setSelected(toggle(selected, c.name, e.target.checked, data.capabilities))}
                />
              ))}
            </fieldset>
          ))}
        </div>
      </Field>
    </FormDrawer>
  );
}
