import type { ColumnDef } from "@tanstack/react-table";
import { RefreshCw, Search } from "lucide-react";
import type { LogEntry, LogList, LogSeverity, LogWindow } from "@/types/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { DataTable } from "@/components/data_table";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { formatDateTime } from "@/lib/format";

const SEVERITY: Record<
  LogSeverity,
  { label: string; variant: "secondary" | "success" | "warning" | "destructive" }
> = {
  info: { label: "Info", variant: "secondary" },
  success: { label: "Éxito", variant: "success" },
  warning: { label: "Aviso", variant: "warning" },
  error: { label: "Error", variant: "destructive" },
};

const RANGES: { id: LogWindow; label: string }[] = [
  { id: "1h", label: "1 h" },
  { id: "24h", label: "24 h" },
  { id: "7d", label: "7 d" },
  { id: "all", label: "Todo" },
];

interface LogsTabProps {
  data?: LogList;
  isLoading: boolean;
  isFetching: boolean;
  error: Error | null;
  search: string;
  onSearchChange: (value: string) => void;
  range: LogWindow;
  onRangeChange: (value: LogWindow) => void;
  autoRefresh: boolean;
  onAutoRefreshChange: (value: boolean) => void;
  onRefresh: () => void;
  onLoadMore: () => void;
  selected: LogEntry | null;
  onSelect: (entry: LogEntry | null) => void;
}

export function LogsTab({
  data,
  isLoading,
  isFetching,
  error,
  search,
  onSearchChange,
  range,
  onRangeChange,
  autoRefresh,
  onAutoRefreshChange,
  onRefresh,
  onLoadMore,
  selected,
  onSelect,
}: LogsTabProps) {
  const items = data?.items ?? [];
  const canLoadMore = items.length < (data?.total ?? 0);
  const rangeLabel = RANGES.find((r) => r.id === range)?.label ?? range;

  const columns: ColumnDef<LogEntry, unknown>[] = [
    {
      accessorKey: "created_at",
      header: "Hora",
      cell: (c) => (
        <span className="whitespace-nowrap tabular-nums text-muted-foreground">
          {formatDateTime(c.row.original.created_at)}
        </span>
      ),
    },
    {
      accessorKey: "severity",
      header: "Nivel",
      cell: (c) => {
        const meta = SEVERITY[c.row.original.severity] ?? SEVERITY.info;
        return <Badge variant={meta.variant}>{meta.label}</Badge>;
      },
    },
    {
      accessorKey: "label",
      header: "Evento",
      cell: (c) => <span className="font-medium">{c.row.original.label}</span>,
    },
    {
      accessorKey: "summary",
      header: "Resumen",
      cell: (c) => (
        <span className="block max-w-xl text-muted-foreground">{c.row.original.summary}</span>
      ),
    },
    {
      accessorKey: "entity_label",
      header: "Entidad",
      cell: (c) => (
        <span className="whitespace-nowrap text-xs text-muted-foreground">
          {c.row.original.entity_label ?? "—"}
        </span>
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-3 xl:flex-row xl:items-center xl:justify-between">
        <div className="relative w-full xl:max-w-sm">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={search}
            onChange={(e) => onSearchChange(e.target.value)}
            placeholder="Buscar por evento, entidad o detalle…"
            className="pl-9"
          />
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex rounded-md border p-0.5">
            {RANGES.map((r) => (
              <button
                key={r.id}
                onClick={() => onRangeChange(r.id)}
                className={
                  "rounded px-2.5 py-1 text-xs font-medium transition-colors " +
                  (range === r.id
                    ? "bg-primary text-primary-foreground"
                    : "text-muted-foreground hover:text-foreground")
                }
              >
                {r.label}
              </button>
            ))}
          </div>
          <label className="flex items-center gap-2 text-xs text-muted-foreground">
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(e) => onAutoRefreshChange(e.target.checked)}
            />
            Actualizar cada 15 s
          </label>
          <Button variant="outline" size="sm" onClick={onRefresh} disabled={isFetching}>
            <RefreshCw className={isFetching ? "animate-spin" : undefined} />
            Actualizar
          </Button>
        </div>
      </div>

      <div className="flex items-center justify-between text-xs text-muted-foreground">
        <span>
          {data?.total ?? 0} evento(s) ·{" "}
          {range === "all" ? "todo el historial" : `últimas ${rangeLabel}`}
        </span>
        {data?.generated_at && <span>Actualizado {formatDateTime(data.generated_at)}</span>}
      </div>

      {error ? (
        <p className="text-sm text-destructive">{error.message}</p>
      ) : isLoading ? (
        <p className="text-muted-foreground">Cargando…</p>
      ) : (
        <DataTable
          columns={columns}
          data={items}
          empty="No hay eventos para este filtro."
          onRowClick={onSelect}
        />
      )}

      {canLoadMore && (
        <div className="flex justify-center">
          <Button variant="outline" onClick={onLoadMore} disabled={isFetching}>
            Cargar más
          </Button>
        </div>
      )}

      <Dialog open={selected !== null} onOpenChange={(open) => !open && onSelect(null)}>
        <DialogContent className="max-w-2xl">
          {selected && (
            <>
              <DialogHeader>
                <DialogTitle>{selected.label}</DialogTitle>
                <DialogDescription>{selected.summary}</DialogDescription>
              </DialogHeader>

              <div className="mb-4 flex flex-wrap items-center gap-2">
                <Badge variant={SEVERITY[selected.severity]?.variant ?? "secondary"}>
                  {SEVERITY[selected.severity]?.label ?? selected.severity}
                </Badge>
                <Badge variant="outline">
                  {data?.category_labels[selected.category] ?? selected.category}
                </Badge>
                <Badge variant="outline">{selected.source}</Badge>
              </div>

              <dl className="grid grid-cols-1 gap-3 text-sm sm:grid-cols-2">
                <div>
                  <dt className="text-xs text-muted-foreground">Fecha</dt>
                  <dd>{formatDateTime(selected.created_at)}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted-foreground">Tipo</dt>
                  <dd className="font-mono text-xs">{selected.type}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted-foreground">Entidad</dt>
                  <dd>{selected.entity_label ?? "—"}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted-foreground">ID del evento</dt>
                  <dd className="tabular-nums">{selected.id}</dd>
                </div>
              </dl>

              <div className="mt-4">
                <p className="mb-2 text-xs font-medium text-muted-foreground">Detalles</p>
                <pre className="max-h-72 overflow-auto rounded-md bg-muted p-3 text-xs">
                  {Object.keys(selected.details).length
                    ? JSON.stringify(selected.details, null, 2)
                    : "Sin detalles adicionales"}
                </pre>
              </div>
            </>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
