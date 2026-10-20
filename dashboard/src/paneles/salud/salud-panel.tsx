import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, Play, Square, RotateCw } from "lucide-react";
import { getHealth, llmAction } from "@/lib/api";
import { formatBytes, formatUptime } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/confirm-dialog";

export function SaludPanel() {
  const qc = useQueryClient();
  const { data, isLoading, error, dataUpdatedAt } = useQuery({
    queryKey: ["health"],
    queryFn: getHealth,
    refetchInterval: 10_000,
  });
  const [confirmAction, setConfirmAction] = useState<null | "stop" | "restart">(null);

  const action = useMutation({
    mutationFn: llmAction,
    onSuccess: () => {
      setConfirmAction(null);
      qc.invalidateQueries({ queryKey: ["health"] });
    },
  });

  if (error) return <p className="text-destructive">Error: {(error as Error).message}</p>;
  if (isLoading || !data) return <p className="text-muted-foreground">Cargando…</p>;

  const llmRunning = data.llm.running && data.llm.status === "ok";
  const dbOk = data.database.integrity === "ok";

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Salud</h1>
          <p className="text-sm text-muted-foreground">
            Actualizado {new Date(dataUpdatedAt).toLocaleTimeString("es-CU")}
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={() => qc.invalidateQueries({ queryKey: ["health"] })}>
          <RefreshCw />
          Refrescar
        </Button>
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">Servicio</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-2">
            <span className="text-lg font-semibold">{data.service}</span>
            <div className="flex flex-wrap gap-2 text-sm">
              <Badge variant="secondary">v{data.version}</Badge>
              <Badge variant="outline">uptime {formatUptime(data.uptime_s)}</Badge>
              <Badge variant={data.telegram_channel === "polling" ? "success" : "secondary"}>
                Telegram: {data.telegram_channel}
              </Badge>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">Base de datos</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-2">
            <Badge variant={dbOk ? "success" : "destructive"}>{dbOk ? "íntegra" : "FALLO"}</Badge>
            <span className="text-sm text-muted-foreground">
              {formatBytes(data.database.size_bytes)} · {data.database.migrations.join(", ")}
            </span>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">Cola</CardTitle>
          </CardHeader>
          <CardContent>
            <span className="text-2xl font-semibold tabular-nums">{data.job_queue}</span>
            <span className="ml-2 text-sm text-muted-foreground">trabajos pendientes</span>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader className="flex-row items-center justify-between">
          <div>
            <CardTitle>Motor LLM</CardTitle>
            <p className="text-sm text-muted-foreground">
              {data.llm.provider} · {data.llm.model ?? "sin modelo"} · {data.llm.url}
            </p>
          </div>
          <Badge variant={llmRunning ? "success" : "destructive"}>
            {data.llm.running ? (data.llm.status === "ok" ? "corriendo" : data.llm.status) : "detenido"}
          </Badge>
        </CardHeader>
        <CardContent className="flex flex-wrap gap-2">
          <Button
            size="sm"
            variant="outline"
            disabled={!data.llm.managed || action.isPending}
            onClick={() => action.mutate("start")}
          >
            <Play />
            Iniciar
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={!data.llm.managed || action.isPending}
            onClick={() => setConfirmAction("restart")}
          >
            <RotateCw />
            Reiniciar
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={!data.llm.managed || action.isPending}
            onClick={() => setConfirmAction("stop")}
          >
            <Square />
            Detener
          </Button>
          {!data.llm.managed && (
            <span className="self-center text-sm text-muted-foreground">
              (LLM externo: no gestionado por Cuadra)
            </span>
          )}
        </CardContent>
      </Card>

      <ConfirmDialog
        open={confirmAction !== null}
        onOpenChange={(o) => !o && setConfirmAction(null)}
        title={confirmAction === "stop" ? "Detener el LLM" : "Reiniciar el LLM"}
        description="El bot no podrá responder mientras el motor esté detenido o reiniciando."
        confirmLabel={confirmAction === "stop" ? "Detener" : "Reiniciar"}
        destructive={confirmAction === "stop"}
        loading={action.isPending}
        onConfirm={() => confirmAction && action.mutate(confirmAction)}
      />
    </div>
  );
}
