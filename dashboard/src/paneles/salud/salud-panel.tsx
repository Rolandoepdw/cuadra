import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, Play, Square, RotateCw, Cpu, Check } from "lucide-react";
import { getHealth, getLlmModels, getLlmStatus, llmAction, setLlmCtx, setLlmModel } from "@/lib/api";
import { formatBytes, formatUptime } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Select } from "@/components/ui/select";
import { Slider } from "@/components/ui/slider";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/confirm-dialog";

export function SaludPanel() {
  const qc = useQueryClient();
  const { data, isLoading, error, dataUpdatedAt } = useQuery({
    queryKey: ["health"],
    queryFn: getHealth,
    refetchInterval: 10_000,
  });
  const { data: llm } = useQuery({
    queryKey: ["llm"],
    queryFn: async () => ({ status: await getLlmStatus(), models: await getLlmModels() }),
    refetchInterval: 15_000,
  });
  const [confirmAction, setConfirmAction] = useState<null | "stop" | "restart">(null);
  const ctxDefault = llm?.status.ctx_default ?? 8192;

  const action = useMutation({
    mutationFn: async ({ action }: { action: "start" | "stop" | "restart" }) => {
      if (action === "restart") {
        // Restart also resets the context to the default. When running,
        // setLlmCtx restarts the engine itself; when stopped we start it after.
        if (data?.llm.running) return setLlmCtx(ctxDefault);
        await setLlmCtx(ctxDefault);
        return llmAction("start");
      }
      return llmAction(action);
    },
    onSuccess: () => {
      setConfirmAction(null);
      qc.invalidateQueries({ queryKey: ["health"] });
      qc.invalidateQueries({ queryKey: ["llm"] });
    },
  });

  if (error) return <p className="text-destructive">Error: {(error as Error).message}</p>;
  if (isLoading || !data) return <p className="text-muted-foreground">Cargando…</p>;

  const dbOk = data.database.integrity === "ok";
  const gpu = llm?.status.gpu ?? data.llm.gpu ?? null;

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

      <ModelCard
        managed={data.llm.managed}
        running={data.llm.running}
        statusText={data.llm.status}
        provider={data.llm.provider}
        url={data.llm.url}
        current={llm?.status.model ?? data.llm.model ?? null}
        models={llm?.models.models ?? []}
        ctx={llm?.status.ctx ?? data.llm.ctx ?? null}
        ctxMax={llm?.status.ctx_max ?? data.llm.ctx_max ?? null}
        ctxDefault={ctxDefault}
        pending={action.isPending}
        onStart={() => action.mutate({ action: "start" })}
        onRestart={() => setConfirmAction("restart")}
        onStop={() => setConfirmAction("stop")}
      />

      {gpu && (
        <GpuCard
          gpu={gpu}
          models={llm?.models.models ?? []}
          current={llm?.status.model ?? null}
          running={data.llm.running}
          memory={llm?.status.memory ?? data.llm.memory ?? null}
          processRamMib={llm?.status.process_ram_mib ?? data.llm.process_ram_mib ?? null}
        />
      )}

      <ConfirmDialog
        open={confirmAction !== null}
        onOpenChange={(o) => !o && setConfirmAction(null)}
        title={confirmAction === "stop" ? "Detener el LLM" : "Reiniciar el LLM"}
        description="El bot no podrá responder mientras el motor esté detenido o reiniciando."
        confirmLabel={confirmAction === "stop" ? "Detener" : "Reiniciar"}
        destructive={confirmAction === "stop"}
        loading={action.isPending}
        onConfirm={() => confirmAction && action.mutate({ action: confirmAction })}
      />
    </div>
  );
}

function ModelCard({
  managed,
  running,
  statusText,
  provider,
  url,
  current,
  models,
  ctx,
  ctxMax,
  ctxDefault,
  pending,
  onStart,
  onRestart,
  onStop,
}: {
  managed: boolean;
  running: boolean;
  statusText: string;
  provider: string;
  url: string;
  current: string | null;
  models: { name: string; size_bytes: number; context_max?: number | null }[];
  ctx?: number | null;
  ctxMax?: number | null;
  ctxDefault: number;
  pending: boolean;
  onStart: () => void;
  onRestart: () => void;
  onStop: () => void;
}) {
  const qc = useQueryClient();
  const [choice, setChoice] = useState("");
  const selected = choice || current || "";
  const changed = selected !== (current ?? "");
  const [confirmSwitch, setConfirmSwitch] = useState(false);

  const switchModel = useMutation({
    mutationFn: (model: string) => setLlmModel(model),
    onSuccess: () => {
      setConfirmSwitch(false);
      setChoice("");
      qc.invalidateQueries({ queryKey: ["health"] });
      qc.invalidateQueries({ queryKey: ["llm"] });
    },
  });

  const llmRunning = running && statusText === "ok";
  const selectedModel = models.find((m) => m.name === selected);
  const maxCtx = selectedModel?.context_max ?? (ctxMax && ctxMax >= 512 ? ctxMax : 32768);
  const [ctxValue, setCtxValue] = useState<number>(ctx ?? ctxDefault);
  const ctxChanged = ctxValue !== (ctx ?? ctxDefault);

  // The server value is the source of truth: after restarting/applying (or
  // switching model) the slider reflects it instead of keeping the draft.
  useEffect(() => {
    setCtxValue(ctx ?? ctxDefault);
  }, [ctx, ctxDefault]);

  useEffect(() => {
    setCtxValue((value) => Math.min(value, maxCtx));
  }, [maxCtx]);
  const setCtx = useMutation({
    mutationFn: (value: number) => setLlmCtx(value),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["health"] });
      qc.invalidateQueries({ queryKey: ["llm"] });
    },
  });

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between">
        <div>
          <CardTitle>Motor LLM</CardTitle>
          <p className="text-sm text-muted-foreground">
            {provider} · {current ?? "sin modelo"} · {url}
          </p>
        </div>
        <Badge variant={llmRunning ? "success" : "destructive"}>
          {running ? (statusText === "ok" ? "corriendo" : statusText) : "detenido"}
        </Badge>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {managed && (
          <>
            <div className="flex flex-col gap-2 sm:flex-row sm:items-end">
              <div className="flex flex-1 flex-col gap-2">
                <label className="text-sm font-medium">Modelo</label>
                <Select value={selected} onChange={(e) => setChoice(e.target.value)}>
                  {!models.some((m) => m.name === selected) && current && (
                    <option value={current}>{current}</option>
                  )}
                  {models.map((m) => (
                    <option key={m.name} value={m.name}>
                      {m.name} · {formatBytes(m.size_bytes)}
                    </option>
                  ))}
                </Select>
              </div>
              <Button
                variant={changed ? "default" : "outline"}
                disabled={!changed || pending || switchModel.isPending}
                onClick={() => (llmRunning ? setConfirmSwitch(true) : switchModel.mutate(selected))}
              >
                {switchModel.isPending ? "Cambiando…" : "Cambiar modelo"}
              </Button>
            </div>
            <div className="flex flex-col gap-2">
              <div className="flex items-center justify-between">
                <label className="text-sm font-medium">Contexto (tokens)</label>
                <span className="text-sm tabular-nums text-muted-foreground">
                  {ctxValue.toLocaleString("es-CU")} / {maxCtx.toLocaleString("es-CU")}
                </span>
              </div>
              <Slider
                min={512}
                max={maxCtx}
                step={512}
                value={ctxValue}
                onChange={(e) => setCtxValue(Number(e.target.value))}
                disabled={!managed || pending || setCtx.isPending}
              />
              <div className="flex items-center justify-between text-xs text-muted-foreground">
                <span>512</span>
                <span>Por defecto: {ctxDefault.toLocaleString("es-CU")}</span>
              </div>
            </div>
            <Button
              variant={ctxChanged ? "default" : "outline"}
              disabled={!ctxChanged || pending || setCtx.isPending}
              onClick={() => (llmRunning ? setConfirmSwitch(true) : setCtx.mutate(ctxValue))}
            >
              {setCtx.isPending ? "Aplicando…" : "Aplicar contexto"}
            </Button>
          </>
        )}

        <div className="flex flex-wrap gap-2">
          <Button size="sm" variant="outline" disabled={!managed || pending} onClick={onStart}>
            <Play />
            Iniciar
          </Button>
          <Button size="sm" variant="outline" disabled={!managed || pending} onClick={onRestart}>
            <RotateCw />
            Reiniciar
          </Button>
          <Button size="sm" variant="outline" disabled={!managed || pending} onClick={onStop}>
            <Square />
            Detener
          </Button>
          {!managed && (
            <span className="self-center text-sm text-muted-foreground">
              (LLM externo: no gestionado por Cuadra)
            </span>
          )}
        </div>
      </CardContent>

      <ConfirmDialog
        open={confirmSwitch}
        onOpenChange={(o) => !o && setConfirmSwitch(false)}
        title="Cambiar modelo / contexto"
        description={`Se reiniciará el motor con "${selected}"${ctxChanged ? ` y contexto ${ctxValue}` : ""}. El bot no responderá durante el reinicio.`}
        confirmLabel="Reiniciar y aplicar"
        loading={switchModel.isPending || setCtx.isPending}
        onConfirm={() => {
          const tasks: Promise<unknown>[] = [];
          if (changed) tasks.push(switchModel.mutateAsync(selected));
          if (ctxChanged) tasks.push(setCtx.mutateAsync(Number(ctxValue)));
          Promise.all(tasks).then(() => {
            setConfirmSwitch(false);
            setChoice("");
            qc.invalidateQueries({ queryKey: ["health"] });
            qc.invalidateQueries({ queryKey: ["llm"] });
          });
        }}
      />
    </Card>
  );
}

function GpuCard({
  gpu,
  models,
  current,
  running,
  memory,
  processRamMib,
}: {
  gpu: { name: string; total_mib: number; used_mib: number; free_mib: number };
  models: { name: string; size_bytes: number }[];
  current: string | null;
  running: boolean;
  memory?: {
    model_vram_mib: number;
    model_ram_mib: number;
    model_total_mib: number;
    kv_vram_mib: number;
    kv_ram_mib: number;
    compute_vram_mib: number;
    compute_ram_mib: number;
    vram_total_mib: number;
    ram_total_mib: number;
    offload_pct: number | null;
    layers: { offloaded: number; total: number } | null;
  } | null;
  processRamMib?: number | null;
}) {
  const pct = gpu.total_mib ? Math.round((gpu.used_mib / gpu.total_mib) * 100) : 0;
  const currentSize = models.find((m) => m.name === current)?.size_bytes;
  const currentMib = currentSize ? Math.round(currentSize / (1024 * 1024)) : null;
  const wontFit =
    !running && currentMib !== null && currentMib > gpu.free_mib + 64;

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-sm font-medium text-muted-foreground">
          <Cpu className="size-4" />
          {gpu.name}
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="flex items-baseline justify-between">
          <span className="text-2xl font-semibold tabular-nums">
            {gpu.used_mib} <span className="text-base font-normal text-muted-foreground">MB usados</span>
          </span>
          <span className="text-sm text-muted-foreground">
            {gpu.free_mib} MB libres / {gpu.total_mib} MB
          </span>
        </div>
        <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
          <div
            className="h-full rounded-full bg-primary transition-all"
            style={{ width: `${pct}%` }}
          />
        </div>
        {running && memory && (
          <div className="flex flex-col gap-1 text-sm text-muted-foreground">
            <div className="flex items-center justify-between">
              <span>
                Modelo: {memory.model_vram_mib} MB VRAM + {memory.model_ram_mib} MB RAM · {memory.offload_pct ?? "?"}% en GPU
                {memory.layers ? ` · ${memory.layers.offloaded}/${memory.layers.total} capas` : ""}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span>
                KV: {memory.kv_vram_mib} MB VRAM{memory.kv_ram_mib ? ` + ${memory.kv_ram_mib} MB RAM` : ""}
                {" · "}Compute: {memory.compute_vram_mib} MB VRAM{memory.compute_ram_mib ? ` + ${memory.compute_ram_mib} MB RAM` : ""}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span>
                Total aprox: {memory.vram_total_mib} MB VRAM + {memory.ram_total_mib} MB RAM (mapeado al proceso)
                {processRamMib ? ` · proceso llama-server: ${processRamMib} MB` : ""}
              </span>
            </div>
          </div>
        )}
        {wontFit && (
          <p className="text-sm text-destructive">
            El modelo elegido (~{currentMib} MB) probablemente no cabe en {gpu.free_mib} MB libres.
          </p>
        )}
        {running && (
          <p className="flex items-center gap-1 text-sm text-muted-foreground">
            <Check className="size-4 text-green-600" />
            La VRAM incluye el modelo cargado; detén el motor para liberarla.
          </p>
        )}
      </CardContent>
    </Card>
  );
}
