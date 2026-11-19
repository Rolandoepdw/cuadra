import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Check, RefreshCw, Save, Send, Wallet, X } from "lucide-react";
import {
  approveSale,
  cancelSale,
  dispatchSale,
  getFinanceSummary,
  getFollowupSettings,
  getSalesSettings,
  listFollowups,
  listLedger,
  listSales,
  muteSaleFollowup,
  sendSaleFollowup,
  setFollowupSettings,
  setSalesSettings,
  settleSale,
} from "@/lib/api";
import { formatDateTime, formatMoney, SALE_STATUS_LABELS, SALE_STATUS_VARIANTS } from "@/lib/format";
import type {
  FollowupMode,
  LedgerEntry,
  LedgerStatus,
  Sale,
  SaleFollowup,
  SalesMode,
} from "@/types/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Select } from "@/components/ui/select";
import { Label } from "@/components/ui/label";
import { DataTable } from "@/components/data_table";
import { ConfirmDialog } from "@/components/confirm_dialog";

const MODE_LABELS: Record<SalesMode, string> = {
  assisted: "Asistido",
  hybrid: "Híbrido",
  autonomous: "Autónomo",
};

const MODE_DESCRIPTIONS: Record<SalesMode, string> = {
  assisted:
    "El bot prepara la venta y tú apruebas, despachas y cobras. Úsalo si quieres revisar cada venta.",
  hybrid:
    "El bot despacha solo si el producto es reciente y el TCP está disponible; si no, pasa a aprobación.",
  autonomous:
    "El bot despacha sin aprobación y tú cobras al final. Es el modo por defecto; úsalo con el catálogo validado.",
};

const FOLLOWUP_MODE_LABELS: Record<FollowupMode, string> = {
  off: "Apagado",
  shadow: "Solo avisar en el panel",
  on: "Activo",
};

const FOLLOWUP_MODE_DESCRIPTIONS: Record<FollowupMode, string> = {
  off: "No se escribe al cliente. Solo ves las ventas estancadas aquí.",
  shadow:
    "No envía nada: registra qué recordatorios se enviarían, para revisarlos antes de activarlo.",
  on: "Envía un único recordatorio por venta en CONFIRMED o NEEDS_OPERATOR, de 09:00 a 20:00.",
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

function formatAge(hours: number): string {
  if (hours < 24) return `${Math.round(hours)} h`;
  const days = Math.floor(hours / 24);
  const rest = Math.round(hours % 24);
  return rest > 0 ? `${days} d ${rest} h` : `${days} d`;
}

function canRemind(sale: SaleFollowup): boolean {
  return (
    (sale.status === "CONFIRMED" || sale.status === "NEEDS_OPERATOR") &&
    !sale.followup_sent_at
  );
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
  const [followupHours, setFollowupHours] = useState(24);
  const followups = useQuery({
    queryKey: ["finance-followups", followupHours],
    queryFn: () => listFollowups(followupHours),
    refetchInterval: 15_000,
  });

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["finance-summary"] });
    qc.invalidateQueries({ queryKey: ["finance-sales"] });
    qc.invalidateQueries({ queryKey: ["finance-ledger"] });
    qc.invalidateQueries({ queryKey: ["finance-followups"] });
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
  const [mode, setMode] = useState<SalesMode>("autonomous");
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

  const followupSettings = useQuery({
    queryKey: ["followup-settings"],
    queryFn: getFollowupSettings,
  });
  const [followupMode, setFollowupMode] = useState<FollowupMode>("shadow");
  useEffect(() => {
    if (followupSettings.data) setFollowupMode(followupSettings.data.mode);
  }, [followupSettings.data]);
  const saveFollowupMode = useMutation({
    mutationFn: () => setFollowupSettings({ mode: followupMode }),
    onSuccess: () => {
      setActionError(null);
      qc.invalidateQueries({ queryKey: ["followup-settings"] });
      refresh();
    },
    onError: onActionError,
  });
  const remindNow = useMutation({
    mutationFn: sendSaleFollowup,
    onSuccess: onActionSuccess,
    onError: onActionError,
  });
  const muteFollowup = useMutation({
    mutationFn: (saleId: number) => muteSaleFollowup(saleId, true),
    onSuccess: onActionSuccess,
    onError: onActionError,
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

      <Card>
        <CardHeader>
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div>
              <CardTitle className="text-base">Seguimiento</CardTitle>
              <p className="text-xs text-muted-foreground">
                Ventas abiertas sin movimiento real (cambio de estado o mensaje del
                cliente). Desde aquí decides si se le recuerda algo al cliente.
              </p>
            </div>
            <div className="flex items-center gap-2">
              <Label className="text-xs">Sin cambios hace</Label>
              <Select
                value={String(followupHours)}
                onChange={(event) => setFollowupHours(Number(event.target.value))}
              >
                {[12, 24, 48, 72].map((value) => (
                  <option key={value} value={value}>
                    {value} h
                  </option>
                ))}
              </Select>
            </div>
          </div>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <div className="flex flex-col gap-2 rounded-md border bg-muted/30 px-3 py-2 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex flex-col">
              <Label className="text-xs">Recordatorio al cliente</Label>
              <span className="text-xs text-muted-foreground">
                {FOLLOWUP_MODE_DESCRIPTIONS[followupMode]}
              </span>
            </div>
            <div className="flex items-center gap-2">
              <Select
                value={followupMode}
                onChange={(event) =>
                  setFollowupMode(event.target.value as FollowupMode)
                }
              >
                {(Object.keys(FOLLOWUP_MODE_LABELS) as FollowupMode[]).map(
                  (value) => (
                    <option key={value} value={value}>
                      {FOLLOWUP_MODE_LABELS[value]}
                    </option>
                  ),
                )}
              </Select>
              <Button
                size="sm"
                onClick={() => saveFollowupMode.mutate()}
                disabled={
                  followupSettings.isLoading ||
                  !followupSettings.data ||
                  followupMode === followupSettings.data.mode ||
                  saveFollowupMode.isPending
                }
              >
                <Save /> Guardar
              </Button>
            </div>
          </div>

          {followups.isLoading ? (
            <p className="text-sm text-muted-foreground">Cargando…</p>
          ) : followups.data && followups.data.items.length > 0 ? (
            <ul className="flex flex-col gap-2">
              {followups.data.items.map((sale) => (
                <li
                  key={sale.id}
                  className="flex flex-wrap items-center justify-between gap-2 rounded-md border px-3 py-2"
                >
                  <div className="flex flex-col">
                    <span className="font-mono text-xs">
                      {sale.order_code ?? `#${sale.id}`}
                    </span>
                    <span className="text-xs text-muted-foreground">
                      {sale.product_name ?? `Producto ${sale.product_id}`} ·{" "}
                      {sale.supplier_name ?? `TCP ${sale.supplier_id}`}
                    </span>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge variant={SALE_STATUS_VARIANTS[sale.status]}>
                      {SALE_STATUS_LABELS[sale.status]}
                    </Badge>
                    <span className="text-xs text-amber-600">
                      hace {formatAge(sale.age_hours)}
                    </span>
                    {!!sale.followup_sent_at && (
                      <Badge variant="success">Recordado</Badge>
                    )}
                    {!!sale.followup_muted_at && (
                      <Badge variant="outline">Silenciado</Badge>
                    )}
                    {canRemind(sale) && (
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => remindNow.mutate(sale.id)}
                        disabled={remindNow.isPending}
                      >
                        <Send /> Recordar
                      </Button>
                    )}
                    {!sale.followup_sent_at && !sale.followup_muted_at && (
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => muteFollowup.mutate(sale.id)}
                        disabled={muteFollowup.isPending}
                      >
                        <X /> Silenciar
                      </Button>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-muted-foreground">Sin ventas estancadas.</p>
          )}
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
        description={
          cancelTarget &&
          (cancelTarget.status === "DISPATCHED" || cancelTarget.status === "CONFIRMED")
            ? "Se libera el stock reservado y la comisión queda anulada. Se avisará al cliente y también al TCP para que no la prepare. No se puede deshacer."
            : "Se libera el stock reservado y la comisión queda anulada. Se avisará al cliente de la cancelación. No se puede deshacer."
        }
        confirmLabel="Cancelar venta"
        destructive
        loading={cancel.isPending}
        onConfirm={() => cancelTarget && cancel.mutate(cancelTarget)}
      />
    </div>
  );
}
