import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Check, RefreshCw, Save, Send, Wallet, X } from "lucide-react";
import {
  approveSale,
  cancelSale,
  dispatchSale,
  getFinanceSummary,
  getSalesSettings,
  listLedger,
  listSales,
  setSalesSettings,
  settleSale,
} from "@/lib/api";
import { formatDateTime, formatMoney } from "@/lib/format";
import type {
  LedgerEntry,
  LedgerStatus,
  Sale,
  SaleStatus,
  SalesMode,
} from "@/types/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Select } from "@/components/ui/select";
import { Label } from "@/components/ui/label";
import { DataTable } from "@/components/data_table";
import { ConfirmDialog } from "@/components/confirm_dialog";

const SALE_STATUS_LABELS: Record<SaleStatus, string> = {
  DRAFT: "Borrador",
  PENDING_APPROVAL: "Por aprobar",
  PENDING_DISPATCH: "Por despachar",
  DISPATCHED: "Despachada",
  CONFIRMED: "Confirmada por TCP",
  DELIVERED: "Entregada",
  SETTLED: "Comisión cobrada",
  CANCELLED: "Cancelada",
  NEEDS_OPERATOR: "Requiere operador",
};

const SALE_STATUS_VARIANTS: Record<
  SaleStatus,
  "success" | "warning" | "secondary" | "destructive" | "outline"
> = {
  DRAFT: "outline",
  PENDING_APPROVAL: "warning",
  PENDING_DISPATCH: "warning",
  DISPATCHED: "secondary",
  CONFIRMED: "secondary",
  DELIVERED: "success",
  SETTLED: "success",
  CANCELLED: "destructive",
  NEEDS_OPERATOR: "destructive",
};

const MODE_LABELS: Record<SalesMode, string> = {
  assisted: "Asistido",
  hybrid: "Híbrido",
  autonomous: "Autónomo",
};

const MODE_DESCRIPTIONS: Record<SalesMode, string> = {
  assisted:
    "El bot prepara la venta y tú apruebas, despachas y cobras. Es el modo seguro por defecto.",
  hybrid:
    "El bot despacha solo si el producto es reciente y el TCP está disponible; si no, pasa a aprobación.",
  autonomous:
    "El bot despacha sin aprobación y tú cobras al final. Úsalo solo con el catálogo validado.",
};

const LEDGER_STATUS_LABELS: Record<LedgerStatus, string> = {
  PENDING: "Pendiente",
  COLLECTED: "Cobrada",
  VOID: "Anulada",
};

const LEDGER_STATUS_VARIANTS: Record<
  LedgerStatus,
  "success" | "warning" | "destructive"
> = {
  PENDING: "warning",
  COLLECTED: "success",
  VOID: "destructive",
};

function isCancellable(sale: Sale): boolean {
  return !["DELIVERED", "SETTLED", "CANCELLED"].includes(sale.status);
}

export function FinancePanel() {
  const qc = useQueryClient();
  const [status, setStatus] = useState("");
  const [currency, setCurrency] = useState("");
  const [cancelTarget, setCancelTarget] = useState<Sale | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const summary = useQuery({
    queryKey: ["finance-summary"],
    queryFn: getFinanceSummary,
    refetchInterval: 15_000,
  });
  const sales = useQuery({
    queryKey: ["finance-sales", status, currency],
    queryFn: () => listSales({ status, currency, limit: 100 }),
    refetchInterval: 15_000,
  });
  const ledger = useQuery({
    queryKey: ["finance-ledger", currency],
    queryFn: () => listLedger({ currency, limit: 100 }),
  });

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["finance-summary"] });
    qc.invalidateQueries({ queryKey: ["finance-sales"] });
    qc.invalidateQueries({ queryKey: ["finance-ledger"] });
  };

  const onActionError = (error: unknown) => setActionError((error as Error).message);
  const onActionSuccess = () => {
    setActionError(null);
    refresh();
  };

  const salesSettings = useQuery({
    queryKey: ["sales-settings"],
    queryFn: getSalesSettings,
  });
  const [mode, setMode] = useState<SalesMode>("assisted");
  useEffect(() => {
    if (salesSettings.data) setMode(salesSettings.data.mode);
  }, [salesSettings.data]);
  const [pendingMode, setPendingMode] = useState<SalesMode | null>(null);
  const saveMode = useMutation({
    mutationFn: () => setSalesSettings({ mode }),
    onSuccess: () => {
      setActionError(null);
      setPendingMode(null);
      qc.invalidateQueries({ queryKey: ["sales-settings"] });
      refresh();
    },
    onError: (error) => {
      setPendingMode(null);
      onActionError(error);
    },
  });

  const approve = useMutation({
    mutationFn: approveSale,
    onSuccess: onActionSuccess,
    onError: onActionError,
  });
  const dispatch = useMutation({
    mutationFn: dispatchSale,
    onSuccess: onActionSuccess,
    onError: onActionError,
  });
  const settle = useMutation({
    mutationFn: settleSale,
    onSuccess: onActionSuccess,
    onError: onActionError,
  });
  const cancel = useMutation({
    mutationFn: (sale: Sale) => cancelSale(sale.id, "Cancelada desde Finanzas"),
    onSuccess: () => {
      setCancelTarget(null);
      onActionSuccess();
    },
    onError: onActionError,
  });

  const busy =
    approve.isPending || dispatch.isPending || settle.isPending || cancel.isPending;

  const salesColumns: ColumnDef<Sale, unknown>[] = [
    {
      accessorKey: "order_code",
      header: "Orden",
      cell: ({ row }) => (
        <div className="flex flex-col">
          <span className="font-mono text-xs">{row.original.order_code ?? "—"}</span>
          <span className="text-xs text-muted-foreground">
            {row.original.product_name ?? `Producto ${row.original.product_id}`}
          </span>
        </div>
      ),
    },
    {
      accessorKey: "recipient_name",
      header: "Cliente",
      cell: ({ row }) => (
        <div className="flex flex-col">
          <span>{row.original.recipient_name ?? "—"}</span>
          <span className="text-xs text-muted-foreground">
            {row.original.customer_phone ?? "Sin teléfono"}
          </span>
        </div>
      ),
    },
    {
      id: "supplier",
      header: "TCP / Zona",
      cell: ({ row }) => (
        <div className="flex flex-col">
          <span>{row.original.supplier_name ?? "—"}</span>
          <span className="text-xs text-muted-foreground">
            {row.original.delivery_zone ?? "Sin zona"}
          </span>
        </div>
      ),
    },
    {
      accessorKey: "amount",
      header: "Total",
      cell: ({ row }) => formatMoney(row.original.amount, row.original.currency),
    },
    {
      accessorKey: "commission_earned",
      header: "Comisión",
      cell: ({ row }) => {
        const sale = row.original;
        const amount = sale.commission_earned ?? sale.commission;
        const commissionCurrency = sale.ledger_currency ?? sale.commission_currency;
        const mismatch = commissionCurrency !== sale.currency;
        return (
          <div className="flex flex-col">
            <span>{formatMoney(amount, commissionCurrency)}</span>
            {mismatch && (
              <span className="text-xs text-amber-600">requiere conversión</span>
            )}
          </div>
        );
      },
    },
    {
      accessorKey: "status",
      header: "Estado",
      cell: ({ row }) => (
        <Badge variant={SALE_STATUS_VARIANTS[row.original.status]}>
          {SALE_STATUS_LABELS[row.original.status]}
        </Badge>
      ),
    },
    {
      accessorKey: "created_at",
      header: "Creada",
      cell: ({ row }) => (
        <span className="text-xs">{formatDateTime(row.original.created_at)}</span>
      ),
    },
    {
      id: "actions",
      header: "Acciones",
      enableSorting: false,
      cell: ({ row }) => {
        const sale = row.original;
        return (
          <div className="flex flex-wrap gap-1">
            {sale.status === "PENDING_APPROVAL" && (
              <Button size="sm" onClick={() => approve.mutate(sale.id)} disabled={busy}>
                <Check /> Aprobar
              </Button>
            )}
            {["PENDING_DISPATCH", "NEEDS_OPERATOR"].includes(sale.status) && (
              <Button
                size="sm"
                variant="outline"
                onClick={() => dispatch.mutate(sale.id)}
                disabled={busy}
              >
                <Send /> Despachar
              </Button>
            )}
            {sale.status === "DELIVERED" && (
              <Button size="sm" onClick={() => settle.mutate(sale.id)} disabled={busy}>
                <Wallet /> Cobrar comisión
              </Button>
            )}
            {isCancellable(sale) && (
              <Button
                size="sm"
                variant="destructive"
                onClick={() => setCancelTarget(sale)}
                disabled={busy}
              >
                <X /> Cancelar
              </Button>
            )}
          </div>
        );
      },
    },
  ];

  const ledgerColumns: ColumnDef<LedgerEntry, unknown>[] = [
    {
      accessorKey: "order_code",
      header: "Orden",
      cell: ({ row }) => (
        <span className="font-mono text-xs">{row.original.order_code ?? "—"}</span>
      ),
    },
    {
      accessorKey: "product_name",
      header: "Producto",
    },
    {
      accessorKey: "supplier_name",
      header: "TCP",
      cell: ({ row }) => row.original.supplier_name ?? "—",
    },
    {
      accessorKey: "sale_total",
      header: "Venta",
      cell: ({ row }) =>
        formatMoney(row.original.sale_total, row.original.currency),
    },
    {
      accessorKey: "commission_earned",
      header: "Comisión",
      cell: ({ row }) => {
        const entry = row.original;
        const mismatch = entry.commission_currency !== entry.currency;
        return (
          <div className="flex flex-col">
            <span>
              {formatMoney(entry.commission_earned, entry.commission_currency)}
            </span>
            {mismatch && (
              <span className="text-xs text-amber-600">requiere conversión</span>
            )}
          </div>
        );
      },
    },
    {
      accessorKey: "supplier_payout",
      header: "Pago al TCP",
      cell: ({ row }) =>
        row.original.supplier_payout === null
          ? "—"
          : formatMoney(row.original.supplier_payout, row.original.currency),
    },
    {
      accessorKey: "status",
      header: "Estado",
      cell: ({ row }) => (
        <Badge variant={LEDGER_STATUS_VARIANTS[row.original.status]}>
          {LEDGER_STATUS_LABELS[row.original.status]}
        </Badge>
      ),
    },
    {
      accessorKey: "collected_at",
      header: "Cobrada",
      cell: ({ row }) => (
        <span className="text-xs">{formatDateTime(row.original.collected_at)}</span>
      ),
    },
  ];

  if (summary.error) {
    return (
      <p className="text-destructive">Error: {(summary.error as Error).message}</p>
    );
  }

  const data = summary.data;
  const totals = data?.totals_by_currency ?? [];

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Finanzas</h1>
          <p className="text-sm text-muted-foreground">
            Ventas, despacho al TCP y comisiones. El pago al cliente se cobra en la entrega.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {data && <Badge variant="secondary">Modo {MODE_LABELS[data.mode]}</Badge>}
          <Button variant="outline" size="sm" onClick={refresh}>
            <RefreshCw /> Refrescar
          </Button>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Ventas hoy
            </CardTitle>
          </CardHeader>
          <CardContent className="text-2xl font-semibold">
            {data?.sales_today ?? "—"}
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Por aprobar
            </CardTitle>
          </CardHeader>
          <CardContent className="text-2xl font-semibold text-amber-600">
            {data?.sales_pending_approval ?? "—"}
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Por despachar
            </CardTitle>
          </CardHeader>
          <CardContent className="text-2xl font-semibold text-amber-600">
            {data?.sales_pending_dispatch ?? "—"}
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              En despacho
            </CardTitle>
          </CardHeader>
          <CardContent className="text-2xl font-semibold">
            {data?.sales_dispatched ?? "—"}
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Entregadas por liquidar
            </CardTitle>
          </CardHeader>
          <CardContent className="text-2xl font-semibold">
            {data?.sales_delivered_pending_settlement ?? "—"}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Modo de venta</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <div className="flex flex-col gap-2 sm:flex-row sm:items-end">
            <div className="flex w-full flex-col gap-2 sm:w-64">
              <Label>Modo</Label>
              <Select
                value={mode}
                onChange={(event) => setMode(event.target.value as SalesMode)}
              >
                {(Object.keys(MODE_LABELS) as SalesMode[]).map((value) => (
                  <option key={value} value={value}>
                    {MODE_LABELS[value]}
                  </option>
                ))}
              </Select>
            </div>
            <Button
              onClick={() => setPendingMode(mode)}
              disabled={
                salesSettings.isLoading ||
                !salesSettings.data ||
                mode === salesSettings.data.mode
              }
            >
              <Save /> Guardar modo
            </Button>
          </div>
          <p className="text-xs text-muted-foreground">{MODE_DESCRIPTIONS[mode]}</p>
        </CardContent>
      </Card>

      {!!data?.sales_needs_operator && (
        <p className="text-sm text-amber-600">
          {data.sales_needs_operator} venta(s) requieren tu decisión (TCP agotado o error de despacho).
        </p>
      )}
      {!!data?.conversion_pending && (
        <p className="text-sm text-amber-600">
          {data.conversion_pending} comisión(es) están en otra moneda y requieren conversión;
          no se suman a los totales.
        </p>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Totales por moneda</CardTitle>
        </CardHeader>
        <CardContent>
          {totals.length === 0 ? (
            <p className="text-sm text-muted-foreground">Sin ventas registradas.</p>
          ) : (
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              {totals.map((total) => (
                <div key={total.currency} className="rounded-lg border p-4">
                  <div className="mb-2 flex items-center justify-between">
                    <span className="font-semibold">{total.currency}</span>
                    <span className="text-sm text-muted-foreground">
                      Venta bruta {formatMoney(total.gross_sales, total.currency)}
                    </span>
                  </div>
                  <dl className="grid grid-cols-3 gap-2 text-sm">
                    <div>
                      <dt className="text-muted-foreground">Generada</dt>
                      <dd>{formatMoney(total.commission_earned, total.currency)}</dd>
                    </div>
                    <div>
                      <dt className="text-muted-foreground">Cobrada</dt>
                      <dd className="text-primary">
                        {formatMoney(total.commission_collected, total.currency)}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-muted-foreground">Pendiente</dt>
                      <dd className="text-amber-600">
                        {formatMoney(total.commission_pending, total.currency)}
                      </dd>
                    </div>
                  </dl>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {actionError && <p className="text-sm text-destructive">{actionError}</p>}

      <div className="flex flex-wrap items-center gap-3">
        <div className="w-48">
          <Select value={status} onChange={(event) => setStatus(event.target.value)}>
            <option value="">Todos los estados</option>
            {Object.entries(SALE_STATUS_LABELS).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </Select>
        </div>
        <div className="w-32">
          <Select value={currency} onChange={(event) => setCurrency(event.target.value)}>
            <option value="">Toda moneda</option>
            <option value="USD">USD</option>
            <option value="CUP">CUP</option>
          </Select>
        </div>
      </div>

      <section className="flex flex-col gap-3">
        <h2 className="text-lg font-semibold">Ventas</h2>
        <DataTable
          columns={salesColumns}
          data={sales.data?.items ?? []}
          empty={sales.isLoading ? "Cargando…" : "No hay ventas con ese filtro."}
        />
      </section>

      <section className="flex flex-col gap-3">
        <h2 className="text-lg font-semibold">Libro de comisiones</h2>
        <DataTable
          columns={ledgerColumns}
          data={ledger.data?.items ?? []}
          empty={ledger.isLoading ? "Cargando…" : "Todavía no hay comisiones."}
        />
      </section>

      <ConfirmDialog
        open={pendingMode !== null}
        onOpenChange={(open) => !open && setPendingMode(null)}
        title={`Cambiar modo a ${pendingMode ? MODE_LABELS[pendingMode] : ""}`}
        description={
          pendingMode
            ? `${MODE_DESCRIPTIONS[pendingMode]} El cambio aplica a las ventas nuevas; las que ya están abiertas siguen su flujo.`
            : undefined
        }
        confirmLabel="Cambiar modo"
        destructive={pendingMode === "autonomous"}
        loading={saveMode.isPending}
        onConfirm={() => saveMode.mutate()}
      />

      <ConfirmDialog
        open={cancelTarget !== null}
        onOpenChange={(open) => !open && setCancelTarget(null)}
        title={`Cancelar ${cancelTarget?.order_code ?? "venta"}`}
        description="Se libera el stock reservado y la comisión queda anulada. No se puede deshacer."
        confirmLabel="Cancelar venta"
        destructive
        loading={cancel.isPending}
        onConfirm={() => cancelTarget && cancel.mutate(cancelTarget)}
      />
    </div>
  );
}
