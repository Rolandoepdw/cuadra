import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Code, Check, X, ArrowUpDown } from "lucide-react";
import { listTraces } from "@/lib/api";
import type { TraceEntry } from "@/types/api";
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

const PAGE_SIZE = 50;

export function TracesPanel() {
  const [methodFilter, setMethodFilter] = useState("");
  const [okFilter, setOkFilter] = useState<boolean | undefined>(undefined);
  const [limit, setLimit] = useState(PAGE_SIZE);
  const [selected, setSelected] = useState<TraceEntry | null>(null);

  const { data, isLoading, isFetching, error, refetch } = useQuery({
    queryKey: ["traces", methodFilter, okFilter, limit],
    queryFn: () =>
      listTraces({
        method: methodFilter || undefined,
        ok: okFilter,
        limit,
      }),
    placeholderData: (previous) => previous,
    refetchInterval: 10000,
  });

  const items = data?.items ?? [];
  const canLoadMore = items.length < (data?.total ?? 0);

  const columns = [
    {
      accessorKey: "timestamp",
      header: "Hora",
      cell: (c: any) => (
        <span className="whitespace-nowrap tabular-nums text-muted-foreground">
          {formatDateTime(c.row.original.timestamp)}
        </span>
      ),
    },
    {
      accessorKey: "method",
      header: "Método",
      cell: (c: any) => (
        <span className="whitespace-nowrap font-mono text-xs text-muted-foreground">
          {c.row.original.method}
        </span>
      ),
    },
    {
      accessorKey: "ok",
      header: "Estado",
      cell: (c: any) => (
        <Badge variant={c.row.original.ok ? "success" : "destructive"}>
          {c.row.original.ok ? "OK" : "Error"}
        </Badge>
      ),
    },
    {
      accessorKey: "duration_ms",
      header: "Duración",
      cell: (c: any) => (
        <span className="tabular-nums text-muted-foreground">
          {c.row.original.duration_ms != null ? `${c.row.original.duration_ms} ms` : "—"}
        </span>
      ),
    },
    {
      accessorKey: "conversation_id",
      header: "Conversación",
      cell: (c: any) => (
        <span className="whitespace-nowrap tabular-nums text-muted-foreground">
          {c.row.original.conversation_id ?? "—"}
        </span>
      ),
    },
    {
      accessorKey: "turn_id",
      header: "Turno",
      cell: (c: any) => (
        <span className="whitespace-nowrap tabular-nums text-muted-foreground">
          {c.row.original.turn_id ?? "—"}
        </span>
      ),
    },
  ];

  const jsonStringify = (value: any) => JSON.stringify(value, null, 2);

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Trazas de ejecución</h1>
        <p className="text-sm text-muted-foreground">
          Input/output de cada paso del agente para diagnosticar fallos y entender el flujo.
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-4">
        <div className="relative w-full xl:max-w-md">
          <Code className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={methodFilter}
            onChange={(e) => setMethodFilter(e.target.value)}
            placeholder="Filtrar por método (ej. pipeline.handle_turn)"
            className="pl-9"
          />
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant={okFilter === true ? "outline" : "default"}
            size="sm"
            onClick={() => setOkFilter(true)}
          >
            <Check className="h-3 w-3 mr-1" /> OK
          </Button>
          <Button
            variant={okFilter === false ? "outline" : "default"}
            size="sm"
            onClick={() => setOkFilter(false)}
          >
            <X className="h-3 w-3 mr-1" /> Error
          </Button>
          <Button variant="outline" size="sm" onClick={() => setOkFilter(undefined)}>
            Todos
          </Button>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => refetch()} disabled={isFetching}>
            <ArrowUpDown className={isFetching ? "animate-spin" : undefined} />
            Actualizar
          </Button>
        </div>
      </div>

      <div className="flex items-center justify-between text-xs text-muted-foreground">
        <span>
          {data?.total ?? 0} traza(s) · {items.length} mostradas
        </span>
        {error && <span className="text-destructive">{error.message}</span>}
      </div>

      {error ? (
        <p className="text-sm text-destructive">{error.message}</p>
      ) : isLoading ? (
        <p className="text-muted-foreground">Cargando…</p>
      ) : (
        <DataTable
          columns={columns}
          data={items}
          empty="No hay trazas con los filtros seleccionados."
          onRowClick={setSelected}
        />
      )}

      {canLoadMore && (
        <div className="flex justify-center">
          <Button
            variant="outline"
            onClick={() => setLimit((l) => l + PAGE_SIZE)}
            disabled={isFetching}
          >
            Cargar más
          </Button>
        </div>
      )}

      <Dialog open={selected !== null} onOpenChange={(open) => !open && setSelected(null)}>
        <DialogContent className="max-w-[90vw] max-h-[90vh]">
          {selected && (
            <>
              <DialogHeader>
                <DialogTitle>{selected.method}</DialogTitle>
                <DialogDescription>
                  {selected.ok ? "Ejecutado correctamente" : "Falló"}
                  {selected.duration_ms != null ? ` · ${selected.duration_ms} ms` : ""}
                </DialogDescription>
              </DialogHeader>

              <div className="mt-4 grid grid-cols-1 gap-6">
                <div>
                  <p className="mb-1 text-xs font-medium text-muted-foreground">Entrada</p>
                  <pre className="rounded-md bg-muted p-3 text-xs overflow-auto">
                    {jsonStringify(selected.input)}
                  </pre>
                </div>
                <div>
                  <p className="mb-1 text-xs font-medium text-muted-foreground">Salida</p>
                  <pre className="rounded-md bg-muted p-3 text-xs overflow-auto">
                    {jsonStringify(selected.output)}
                  </pre>
                </div>
              </div>

              {(selected.error || selected.error_type) && (
                <div className="mt-4 rounded-lg bg-destructive/10 border border-destructive/20 p-3">
                  <p className="mb-1 text-xs font-medium text-destructive">Error</p>
                  <pre className="rounded-md bg-destructive/50 p-3 text-xs text-destructive overflow-auto">
                    {selected.error_type}: {selected.error}
                  </pre>
                </div>
              )}
            </>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}

export default TracesPanel;
