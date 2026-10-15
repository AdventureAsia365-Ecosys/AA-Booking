"use client";
// app/(staff)/suppliers/_parts/RatesTab.tsx — the supplier's net rates per contract (AA-823, bk-010; ADR 0005 d2/d8).
// Pick a contract; a draft's rates are added, edited and removed here (supply.write), a published contract's rates
// are read-only (fixed: new prices = a new contract). The maker-checker rule of the contract covers its rates: whoever
// last wrote a rate cannot publish the contract (Contracts tab). Rates of a published contract are what proposal items
// take as their default net.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil, Plus, Trash2 } from "lucide-react";
import { suppliersApi, type SupplierContract, type SupplierDetail } from "@/lib/api/suppliers";
import { supplyApi, type SupplierRate } from "@/lib/api/supply";
import { Button, ConfirmDialog, EmptyState, Section, useToast } from "@/ui";
import { RowMenu } from "@/ui/booking/RowMenu";
import { RatesTable, amountText, rateTarget, supplyError } from "@/app/(staff)/_parts/supply";
import { inputCls } from "@/app/(staff)/_parts/FormField";
import { RateDrawer } from "./RateDrawer";
import { ContractPhaseBadge } from "./shared";

export function RatesTab({ s, canWrite }: { s: SupplierDetail; canWrite: boolean }) {
  const qc = useQueryClient();
  const toast = useToast();
  const contracts = useQuery({ queryKey: ["supplier", s.code, "contracts"], queryFn: () => suppliersApi.contracts(s.code) });
  const items = contracts.data?.items ?? [];
  const [picked, setPicked] = useState<string>("");
  const contract: SupplierContract | undefined =
    items.find((k) => k.id === picked) ?? items.find((k) => k.status === "draft") ?? items.find((k) => k.phase === "in_force") ?? items[0];
  const rates = useQuery({
    queryKey: ["rates", "contract", contract?.id],
    queryFn: () => supplyApi.contractRates(contract!.id),
    enabled: !!contract,
  });
  const [drawer, setDrawer] = useState<{ rate?: SupplierRate } | null>(null);
  const [removing, setRemoving] = useState<SupplierRate | null>(null);
  const remove = useMutation({
    mutationFn: (r: SupplierRate) => supplyApi.deleteRate(r.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["rates"] });
      toast.success("Rate removed from the draft.");
      setRemoving(null);
    },
  });

  if (!contracts.isLoading && items.length === 0) {
    return (
      <Section>
        <EmptyState title="No contract yet" description="Rates are entered under a contract. Add one on the Contracts tab first." />
      </Section>
    );
  }
  const editable = canWrite && contract?.status === "draft";
  return (
    <Section
      padded={false}
      className="p-4"
      title="Net rates"
      description={
        editable
          ? "This contract is a draft: add and correct its rates here. Whoever writes a rate cannot publish the contract."
          : "Rates of a published contract are fixed. New prices are a new contract."
      }
      actions={
        <div className="flex flex-wrap items-center gap-2">
          <label className="sr-only" htmlFor="rates-contract">
            Contract
          </label>
          <select id="rates-contract" className={`${inputCls} h-9 w-auto min-w-[220px]`} value={contract?.id ?? ""} onChange={(e) => setPicked(e.target.value)}>
            {items.map((k) => (
              <option key={k.id} value={k.id}>
                {k.title} · {k.currency} · {k.phase.replace("_", " ")}
              </option>
            ))}
          </select>
          {contract && <ContractPhaseBadge phase={contract.phase} />}
          {editable && s.status === "active" && (
            <Button variant="primary" onClick={() => setDrawer({})}>
              <Plus size={15} /> Add rate
            </Button>
          )}
        </div>
      }
    >
      <RatesTable
        tableId="supplier-rates"
        rates={rates.data?.items ?? []}
        loading={contracts.isLoading || rates.isLoading}
        error={rates.isError ? (rates.error as Error).message : null}
        onRetry={() => rates.refetch()}
        emptyTitle="No rates in this contract"
        emptyDescription={editable ? "Add the net price of each room type or service with Add rate." : "This contract has no rates."}
        menu={
          editable
            ? (r) => (
                <RowMenu
                  label={`Actions for ${rateTarget(r)}`}
                  items={[
                    { label: "Edit rate", icon: <Pencil size={15} />, onSelect: () => setDrawer({ rate: r }) },
                    { label: "Remove", icon: <Trash2 size={15} />, onSelect: () => {
                        remove.reset();
                        setRemoving(r);
                      },
                      danger: true,
                      divider: true,
                    },
                  ]}
                />
              )
            : undefined
        }
      />
      {drawer && contract && <RateDrawer supplierId={s.id} contract={contract} rate={drawer.rate} onClose={() => setDrawer(null)} />}
      <ConfirmDialog
        open={removing !== null}
        onClose={() => setRemoving(null)}
        onConfirm={() => removing && remove.mutate(removing)}
        busy={remove.isPending}
        error={supplyError(remove.error)}
        danger
        title="Remove rate"
        confirmLabel="Remove"
        message={
          <>
            Remove the {removing ? amountText(removing.net_amount, removing.currency) : ""} rate for{" "}
            <strong className="text-ink">{removing ? rateTarget(removing) : ""}</strong> from this draft?
          </>
        }
      />
    </Section>
  );
}
