// ui/booking/PageFrame.tsx
// AA-683 — the scroll region of every staff page (the shell's <main>, class kept from CIS for the
// phone padding rule in ui/shell.css) with the kit PageHeader on top.

import { PageHeader } from "../primitives";

export function PageFrame({
  title,
  description,
  actions,
  breadcrumbs,
  children,
  width = "wide",
}: {
  title: React.ReactNode;
  description?: React.ReactNode;
  actions?: React.ReactNode;
  breadcrumbs?: { label: string; href?: string }[];
  children: React.ReactNode;
  width?: "wide" | "narrow";
}) {
  return (
    <main className="aa-admin-main flex-1 overflow-y-auto px-8 py-6">
      <div className={`mx-auto flex flex-col gap-5 ${width === "narrow" ? "max-w-[880px]" : "max-w-[1440px]"}`}>
        <PageHeader title={title} description={description} actions={actions} breadcrumbs={breadcrumbs} />
        {children}
      </div>
    </main>
  );
}
