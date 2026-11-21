import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  Bug,
  Clock,
  LayoutDashboard,
  ShieldAlert,
  Users,
  Wrench,
  Zap,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { getMetricsSummary, getMetricsTimeline } from "@/lib/api";
import type { MetricsTimelinePoint } from "@/types/api";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  ResponsiveContainer,
  Tooltip,
  CartesianGrid,
} from "recharts";
import { Select } from "@/components/ui/select";
import { Label } from "@/components/ui/label";

export function MetricsPanel() {
  const [days, setDays] = useState("7");

  const { data: summary, error: summaryError } = useQuery({
    queryKey: ["metrics/summary"],
    queryFn: getMetricsSummary,
    refetchInterval: 30000,
  });

  const { data: timelineData, error: timelineError } = useQuery({
    queryKey: ["metrics/timeline", days],
    queryFn: () => getMetricsTimeline(Number(days)),
    refetchInterval: 30000,
  });

  const failedToday = summary?.failed_traces_today ?? 0;
  const queryError = summaryError ?? timelineError;
  const timeline = timelineData?.timeline ?? [];

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Métricas de calidad</h1>
        <p className="text-sm text-muted-foreground">
          Salud del agente: latencia, repeticiones, ventas atascadas y escalaciones.
        </p>
      </div>

      {queryError && (
        <p className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          No se pudieron cargar las métricas: {queryError.message}
        </p>
      )}

      {/* Tarjetas resumen */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <MetricCard
          title="Trazas hoy"
          value={summary?.total_traces_today ?? 0}
          icon={Activity}
          subtitle={failedToday > 0 ? `${failedToday} con error` : "Sin fallos"}
        />
        <MetricCard
          title="Latencia media"
          value={
            summary?.avg_duration_ms != null
              ? `${summary.avg_duration_ms} ms`
              : "—"
          }
          icon={Clock}
          subtitle={
            summary?.avg_llm_ms != null
              ? `Turno completo · LLM ${summary.avg_llm_ms} ms`
              : "Tiempo medio por turno"
          }
        />
        <MetricCard
          title="Ventas atascadas"
          value={summary?.stuck_sales_dispatched ?? 0}
          icon={ShieldAlert}
          subtitle="En DISPATCHED > 30 min"
        />
        <MetricCard
          title="Escalaciones abiertas"
          value={summary?.open_escalations ?? 0}
          icon={Users}
          subtitle="Pendientes o respondidas"
        />
        <MetricCard
          title="Acks ambiguos"
          value={summary?.ambiguous_acks_today ?? 0}
          icon={Bug}
          subtitle="Hoy, TCP sin venta clara"
        />
        <MetricCard
          title="Conversaciones activas"
          value={summary?.active_conversations ?? 0}
          icon={LayoutDashboard}
          subtitle="ACTIVE o WAITING_TCP"
        />
      </div>

      <div className="mt-6">
        <h2 className="text-lg font-semibold">Herramientas</h2>
        <p className="text-sm text-muted-foreground">
          Lecturas que el agente ejecutó hoy: catálogo, ventas, zonas, memoria y
          escalaciones.
        </p>
        <div className="mt-4 grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
          <MetricCard
            title="Llamadas a tools"
            value={summary?.tool_calls_today ?? 0}
            icon={Wrench}
            subtitle={`${summary?.tool_calls_per_turn ?? 0} por turno`}
          />
          <MetricCard
            title="Errores de tools"
            value={summary?.tool_errors_today ?? 0}
            icon={Bug}
            subtitle="Ejecuciones fallidas"
          />
          <MetricCard
            title="Cache hit"
            value={summary?.tool_cache_hits_today ?? 0}
            icon={Zap}
            subtitle="Lecturas repetidas en el turno"
          />
          <MetricCard
            title="Grounding"
            value={summary?.grounding_violations_today ?? 0}
            icon={ShieldAlert}
            subtitle={`${summary?.watchdog_timeouts_today ?? 0} watchdogs`}
          />
        </div>
        {(summary?.top_tools?.length ?? 0) > 0 && (
          <div className="mt-4 rounded-lg border p-4">
            <h3 className="text-sm font-semibold text-muted-foreground mb-2">
              Top tools
            </h3>
            <ul className="space-y-1 text-sm">
              {summary?.top_tools?.map((tool) => (
                <li key={tool.method} className="flex justify-between gap-4">
                  <span className="font-mono text-xs">{tool.method}</span>
                  <span className="text-muted-foreground">{tool.calls}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>

      <div className="mt-6">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <h2 className="text-lg font-semibold">Trazas por día</h2>
          <div className="flex items-center gap-2">
            <Label htmlFor="metrics-days">Días</Label>
            <Select
              id="metrics-days"
              className="w-32"
              value={days}
              onChange={(event) => setDays(event.target.value)}
            >
              {["3", "7", "14", "30"].map((value) => (
                <option key={value} value={value}>
                  {value} días
                </option>
              ))}
            </Select>
          </div>
        </div>

        <div className="mt-4 w-full">
          {timeline.length === 0 ? (
            <p className="rounded-lg border border-dashed px-4 py-10 text-center text-sm text-muted-foreground">
              Sin trazas en los últimos {days} días. En cuanto el agente procese mensajes
              aparecerán aquí.
            </p>
          ) : (
            <ResponsiveContainer width="100%" height={300}>
              <LineChart data={timeline}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                <XAxis dataKey="dia" tick={{ fontSize: 12 }} />
                <YAxis tickFormatter={(v) => String(Math.round(v))} tickCount={5} />
                <Tooltip
                  content={({ active, payload }: any) => {
                    if (!active || !payload || !payload.length) return null;
                    const dataPoint = payload[0].payload as MetricsTimelinePoint;
                    return (
                      <div className="p-2 bg-card border border-border">
                        <p className="text-xs font-bold">{dataPoint.dia}</p>
                        <p className="text-xs text-muted-foreground">
                          {dataPoint.total_traces} trazas
                        </p>
                        <p className="text-xs text-muted-foreground">
                          {dataPoint.failed_traces} fallos
                        </p>
                        <p className="text-xs text-muted-foreground">
                          {dataPoint.avg_duration_ms != null
                            ? `${dataPoint.avg_duration_ms} ms`
                            : "—"}{" "}
                          media
                        </p>
                      </div>
                    );
                  }}
                />
                <Line
                  type="monotone"
                  dataKey="total_traces"
                  name="Trazas"
                  stroke="#3b82f6"
                  strokeWidth={2}
                  dot={{ fill: "#3b82f6", strokeWidth: 2 }}
                />
                <Line
                  type="monotone"
                  dataKey="failed_traces"
                  name="Fallos"
                  stroke="#ef4444"
                  strokeWidth={2}
                  dot={{ fill: "#ef4444", strokeWidth: 2 }}
                />
              </LineChart>
            </ResponsiveContainer>
          )}
        </div>
      </div>

      <div className="mt-6 bg-muted/50 rounded-lg p-4">
        <h3 className="text-sm font-semibold text-muted-foreground mb-3">
          Cómo leer estas métricas
        </h3>
        <ul className="text-sm text-muted-foreground space-y-2">
          <li>
            <span className="font-medium">Trazas hoy:</span> número de ejecuciones de métodos
            rastreadas hoy.
          </li>
          <li>
            <span className="font-medium">Latencia media:</span> tiempo promedio de respuesta del
            método más lento en la cadena de respuesta del cliente.
          </li>
          <li>
            <span className="font-medium">Ventas atascadas:</span> ventas en estado DISPATCHED
            hace más de 30 minutos sin que el TCP haya confirmado (RECIBIDO/ENTREGADO).
          </li>
          <li>
            <span className="font-medium">Escalaciones abiertas:</span> consultas al TCP que aún
            no tienen respuesta.
          </li>
          <li>
            <span className="font-medium">Acks ambiguos:</span> mensajes de TCP donde el sistema
            no pudo determinar a qué venta aplicar el acuse (múltiples ventas DISPATCHED sin
            código de orden).
          </li>
          <li>
            <span className="font-medium">Llamadas a tools:</span> lecturas ejecutadas por el
            agente (catálogo, ventas, zonas, memoria y escalaciones). «Por turno» es el promedio
            de lecturas por turno con tools.
          </li>
          <li>
            <span className="font-medium">Cache hit:</span> lecturas repetidas en el mismo turno
            que se resolvieron desde el cache del TurnFrame.
          </li>
          <li>
            <span className="font-medium">Grounding / watchdogs:</span> afirmaciones sin
            evidencia detectadas en `shadow` y turnos que superaron el watchdog.
          </li>
        </ul>
      </div>
    </div>
  );
}

export default MetricsPanel;

interface MetricCardProps {
  title: string;
  value: string | number;
  icon: React.ComponentType<{ className?: string }>;
  subtitle: string;
}

function MetricCard({ title, value, icon: Icon, subtitle }: MetricCardProps) {
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between py-2">
        <CardTitle className="text-sm font-medium">{title}</CardTitle>
        <Icon className="h-4 w-4 text-muted-foreground" />
      </CardHeader>
      <CardContent>
        <div className="text-2xl font-bold">{value}</div>
        <p className="text-xs text-muted-foreground">{subtitle}</p>
      </CardContent>
    </Card>
  );
}
