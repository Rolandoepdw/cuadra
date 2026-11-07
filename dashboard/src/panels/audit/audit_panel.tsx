import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Check,
  History,
  RefreshCw,
  RotateCcw,
  X,
} from "lucide-react";
import {
  approveCatalogMutation,
  listCatalogMutations,
  listSuppliers,
  listZones,
  rejectCatalogMutation,
  revertCatalogMutation,
} from "@/lib/api";
import { formatDateTime, formatMoney, STATUS_LABELS } from "@/lib/format";
import type {
  CatalogMutation,
  CatalogState,
  MutationStatus,
  MutationType,
} from "@/types/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Select } from "@/components/ui/select";
import { ConfirmDialog } from "@/components/confirm_dialog";

const STATUS_LABELS_AUDIT: Record<MutationStatus, string> = {
  APPLIED: "Aplicada",
  PENDING_REVIEW: "Por revisar",
  REVERTED: "Revertida",
  REJECTED: "Rechazada",
};

const STATUS_VARIANTS: Record<
  MutationStatus,
  "success" | "warning" | "secondary" | "destructive"
> = {
  APPLIED: "success",
  PENDING_REVIEW: "warning",
  REVERTED: "secondary",
  REJECTED: "destructive",
};

const TYPE_LABELS: Record<MutationType, string> = {
  PRICE: "Precio",
  STOCK: "Stock",
  STATUS: "Estado",
  SPECS: "Especificaciones",
  ZONE: "Zonas",
  MIXED: "Varios campos",
};

const METHOD_LABELS: Record<CatalogMutation["correlation_method"], string> = {
  case_id: "Código [E-]",
  single_pending: "Única escalación",
  heuristic: "Heurística",
  manual: "Manual",
};

type DiffRow = {
  label: string;
  before: string;
  after: string;
  changed: boolean;
}

function stateToRows(
  before: CatalogState,
  after: CatalogState,
  zoneNames: Record<number, string>,
): DiffRow[] {
  const zones = (ids: number[]) =>
    ids.length
      ? ids.map((id) => zoneNames[id] ?? `#${id}`).join(", ")
      : "Sin zonas";
  const specs = (state: CatalogState) => {
    const entries = Object.entries(state.specifications ?? {});
    return entries.length
      ? entries.map(([key, value]) => `${key}: ${String(value)}`).join(" · ")
      : "—";
  };
  const rows: DiffRow[] = [
    {
      label: "Precio",
      before: formatMoney(before.price, before.currency),
      after: formatMoney(after.price, after.currency),
      changed: before.price !== after.price || before.currency !== after.currency,
    },
    {
      label: "Stock",
      before: String(before.stock),
      after: String(after.stock),
      changed: before.stock !== after.stock,
    },
    {
      label: "Estado",
      before: STATUS_LABELS[before.status] ?? before.status,
      after: STATUS_LABELS[after.status] ?? after.status,
      changed: before.status !== after.status,
    },
    {
      label: "Especificaciones",
      before: specs(before),
      after: specs(after),
      changed: JSON.stringify(before.specifications) !== JSON.stringify(after.specifications),
    },
    {
      label: "Zonas de entrega",
      before: zones(before.delivery_zone_ids ?? []),
      after: zones(after.delivery_zone_ids ?? []),
      changed:
        JSON.stringify([...before.delivery_zone_ids].sort()) !==
        JSON.stringify([...after.delivery_zone_ids].sort()),
    },
  ];
  return rows;
}

function SnapshotDetails({ label, state }: { label: string; state: CatalogState }) {
  return (
    <details className="rounded-md border bg-muted/30 px-3 py-2 text-xs">
      <summary className="cursor-pointer font-medium">{label}</summary>
      <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-words">
        {JSON.stringify(state, null, 2)}
      </pre>
    </details>
  );
}

function MutationCard({
  mutation,
  zoneNames,
  busy,
  onApprove,
  onReject,
  onRevert,
}: {
  mutation: CatalogMutation;
  zoneNames: Record<number, string>;
  busy: boolean;
  onApprove: (mutation: CatalogMutation) => void;
  onReject: (mutation: CatalogMutation) => void;
  onRevert: (mutation: CatalogMutation) => void;
}) {
  const rows = stateToRows(mutation.previous_state, mutation.new_state, zoneNames);
  const changedRows = rows.filter((row) => row.changed);

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div className="flex flex-col gap-1">
            <CardTitle className="text-base">
              {mutation.product_name ?? `Producto ${mutation.product_id}`}
            </CardTitle>
            <p className="text-xs text-muted-foreground">
              TCP: {mutation.supplier_name ?? mutation.supplier_id} ·{" "}
              {TYPE_LABELS[mutation.mutation_type]} · {formatDateTime(mutation.created_at)}
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-1.5">
            <Badge variant={STATUS_VARIANTS[mutation.status]}>
              {STATUS_LABELS_AUDIT[mutation.status]}
            </Badge>
            {mutation.ambiguous && (
              <Badge variant="destructive">
                <AlertTriangle className="mr-1 h-3 w-3" /> Ambigua
              </Badge>
            )}
            <Badge variant="outline">{METHOD_LABELS[mutation.correlation_method]}</Badge>
            <Badge variant="secondary">
              Confianza {Math.round(mutation.confidence * 100)}%
            </Badge>
          </div>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="grid grid-cols-1 gap-2 lg:grid-cols-2">
          {changedRows.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              La mutación no cambió ningún campo aplicable.
            </p>
          ) : (
            changedRows.map((row) => (
              <div key={row.label} className="rounded-md border p-2 text-sm">
                <div className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  {row.label}
                </div>
                <div className="mt-1 flex flex-wrap items-center gap-2">
                  <span className="text-muted-foreground line-through">{row.before}</span>
                  <span className="text-muted-foreground">→</span>
                  <span className="font-medium">{row.after}</span>
                </div>
              </div>
            ))
          )}
        </div>

        {mutation.source_text && (
          <div className="rounded-md border bg-muted/30 p-3 text-sm">
            <div className="mb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Mensaje del TCP
            </div>
            <p className="whitespace-pre-wrap break-words">{mutation.source_text}</p>
          </div>
        )}

        {mutation.notes && (
          <p className="text-sm text-amber-600">Motivo de revisión: {mutation.notes}</p>
        )}

        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          {mutation.escalation_id && (
            <span className="font-mono">[E-{String(mutation.escalation_id).padStart(4, "0")}]</span>
          )}
          <span>Aplicada por {mutation.applied_by === "bot" ? "el bot" : "el operador"}</span>
          {mutation.reverted_at && (
            <span>
              · Revertida {formatDateTime(mutation.reverted_at)}
              {mutation.revert_reason ? `: ${mutation.revert_reason}` : ""}
            </span>
          )}
        </div>

        <div className="flex flex-wrap gap-2">
          <SnapshotDetails label="Estado anterior" state={mutation.previous_state} />
          <SnapshotDetails label="Estado propuesto" state={mutation.new_state} />
        </div>

        <div className="flex flex-wrap gap-2">
          {mutation.status === "PENDING_REVIEW" && (
            <>
              <Button size="sm" disabled={busy} onClick={() => onApprove(mutation)}>
                <Check /> Aprobar
              </Button>
              <Button
                size="sm"
                variant="destructive"
                disabled={busy}
                onClick={() => onReject(mutation)}
              >
                <X /> Rechazar
              </Button>
            </>
          )}
          {mutation.status === "APPLIED" && (
            <Button
              size="sm"
              variant="outline"
              disabled={busy}
              onClick={() => onRevert(mutation)}
            >
              <RotateCcw /> Deshacer
            </Button>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

export function AuditPanel() {
  const qc = useQueryClient();
  const [status, setStatus] = useState("");
  const [supplierId, setSupplierId] = useState("");
  const [productId, setProductId] = useState("");
  const [ambiguous, setAmbiguous] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [limit, setLimit] = useState(25);
  const [actionError, setActionError] = useState<string | null>(null);
  const [revertTarget, setRevertTarget] = useState<CatalogMutation | null>(null);
  const [rejectTarget, setRejectTarget] = useState<CatalogMutation | null>(null);

  const suppliers = useQuery({ queryKey: ["suppliers"], queryFn: listSuppliers });
  const zones = useQuery({ queryKey: ["zones"], queryFn: listZones });
  const mutations = useQuery({
    queryKey: [
      "catalog-mutations",
      status,
      supplierId,
      productId,
      ambiguous,
      dateFrom,
      dateTo,
      limit,
    ],
    queryFn: () =>
      listCatalogMutations({
        status: status || undefined,
        supplier_id: supplierId ? Number(supplierId) : undefined,
        product_id: productId ? Number(productId) : undefined,
        ambiguous: ambiguous === "" ? undefined : ambiguous === "true",
        date_from: dateFrom || undefined,
        date_to: dateTo || undefined,
        limit,
      }),
  });

  const zoneNames = useMemo(() => {
    const map: Record<number, string> = {};
    for (const zone of zones.data ?? []) map[zone.id] = zone.name;
    return map;
  }, [zones.data]);

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["catalog-mutations"] });
  };

  const onActionError = (error: unknown) => setActionError((error as Error).message);
  const onActionSuccess = () => {
    setActionError(null);
    setRevertTarget(null);
    setRejectTarget(null);
    refresh();
  };

  const approve = useMutation({
    mutationFn: (mutation: CatalogMutation) => approveCatalogMutation(mutation.id),
    onSuccess: onActionSuccess,
    onError: onActionError,
  });
  const reject = useMutation({
    mutationFn: (mutation: CatalogMutation) =>
      rejectCatalogMutation(mutation.id, "Rechazada desde Auditoría"),
    onSuccess: onActionSuccess,
    onError: onActionError,
  });
  const revert = useMutation({
    mutationFn: (mutation: CatalogMutation) =>
      revertCatalogMutation(mutation.id, "Revertida desde Auditoría"),
    onSuccess: onActionSuccess,
    onError: onActionError,
  });

  const busy = approve.isPending || reject.isPending || revert.isPending;
  const items = mutations.data?.items ?? [];
  const total = mutations.data?.total ?? 0;
  const pendingCount = items.filter((item) => item.status === "PENDING_REVIEW").length;

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <History className="h-6 w-6" /> Auditoría de catálogo
          </h1>
          <p className="text-sm text-muted-foreground">
            Cambios que envió un TCP: qué se aplicó, qué espera tu revisión y cómo
            deshacerlos sin corromper el catálogo.
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={refresh}>
          <RefreshCw /> Refrescar
        </Button>
      </div>

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Por revisar (en pantalla)
            </CardTitle>
          </CardHeader>
          <CardContent className="text-2xl font-semibold text-amber-600">
            {pendingCount}
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Mutaciones listadas
            </CardTitle>
          </CardHeader>
          <CardContent className="text-2xl font-semibold">{items.length}</CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Total con filtros
            </CardTitle>
          </CardHeader>
          <CardContent className="text-2xl font-semibold">{total}</CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Ambigüedad
            </CardTitle>
          </CardHeader>
          <CardContent className="text-2xl font-semibold">
            {items.filter((item) => item.ambiguous).length}
          </CardContent>
        </Card>
      </div>

      {actionError && <p className="text-sm text-destructive">{actionError}</p>}
      {mutations.error && (
        <p className="text-sm text-destructive">
          Error: {(mutations.error as Error).message}
        </p>
      )}

      <div className="flex flex-wrap items-end gap-3">
        <div className="w-44">
          <label className="mb-1 block text-xs text-muted-foreground">Estado</label>
          <Select value={status} onChange={(event) => setStatus(event.target.value)}>
            <option value="">Todos</option>
            {Object.entries(STATUS_LABELS_AUDIT).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </Select>
        </div>
        <div className="w-52">
          <label className="mb-1 block text-xs text-muted-foreground">TCP</label>
          <Select
            value={supplierId}
            onChange={(event) => setSupplierId(event.target.value)}
          >
            <option value="">Todos</option>
            {(suppliers.data ?? []).map((supplier) => (
              <option key={supplier.id} value={supplier.id}>
                {supplier.name}
              </option>
            ))}
          </Select>
        </div>
        <div className="w-36">
          <label className="mb-1 block text-xs text-muted-foreground">Producto (ID)</label>
          <input
            type="number"
            min={1}
            value={productId}
            onChange={(event) => setProductId(event.target.value)}
            className="flex h-9 w-full rounded-md border border-input bg-card px-3 py-1 text-sm text-card-foreground shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            placeholder="Todos"
          />
        </div>
        <div className="w-36">
          <label className="mb-1 block text-xs text-muted-foreground">Ambigüedad</label>
          <Select value={ambiguous} onChange={(event) => setAmbiguous(event.target.value)}>
            <option value="">Todas</option>
            <option value="true">Solo ambiguas</option>
            <option value="false">No ambiguas</option>
          </Select>
        </div>
        <div className="w-40">
          <label className="mb-1 block text-xs text-muted-foreground">Desde</label>
          <input
            type="date"
            value={dateFrom}
            onChange={(event) => setDateFrom(event.target.value)}
            className="flex h-9 w-full rounded-md border border-input bg-card px-3 py-1 text-sm text-card-foreground shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
        </div>
        <div className="w-40">
          <label className="mb-1 block text-xs text-muted-foreground">Hasta</label>
          <input
            type="date"
            value={dateTo}
            onChange={(event) => setDateTo(event.target.value)}
            className="flex h-9 w-full rounded-md border border-input bg-card px-3 py-1 text-sm text-card-foreground shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
        </div>
      </div>

      <section className="flex flex-col gap-3">
        {mutations.isLoading ? (
          <p className="text-sm text-muted-foreground">Cargando…</p>
        ) : items.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No hay mutaciones con ese filtro. Cuando un TCP actualice precio, stock o
            especificaciones aparecerán aquí.
          </p>
        ) : (
          items.map((mutation) => (
            <MutationCard
              key={mutation.id}
              mutation={mutation}
              zoneNames={zoneNames}
              busy={busy}
              onApprove={(target) => approve.mutate(target)}
              onReject={setRejectTarget}
              onRevert={setRevertTarget}
            />
          ))
        )}
      </section>

      {items.length < total && (
        <Button variant="outline" onClick={() => setLimit((value) => value + 25)}>
          Mostrar más
        </Button>
      )}

      <ConfirmDialog
        open={revertTarget !== null}
        onOpenChange={(open) => !open && setRevertTarget(null)}
        title="Deshacer mutación"
        description={
          <span>
            Se restaurará el estado anterior de{" "}
            <strong>{revertTarget?.product_name ?? "el producto"}</strong>. Si el
            producto cambió después de esta mutación, la operación se rechaza con
            conflicto y no se toca nada.
          </span>
        }
        confirmLabel="Deshacer"
        destructive
        loading={revert.isPending}
        onConfirm={() => revertTarget && revert.mutate(revertTarget)}
      />

      <ConfirmDialog
        open={rejectTarget !== null}
        onOpenChange={(open) => !open && setRejectTarget(null)}
        title="Rechazar mutación"
        description="La propuesta queda registrada como rechazada y el catálogo no cambia."
        confirmLabel="Rechazar"
        destructive
        loading={reject.isPending}
        onConfirm={() => rejectTarget && reject.mutate(rejectTarget)}
      />
    </div>
  );
}
