import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  Bug,
  Clock,
  LayoutDashboard,
  ShieldAlert,
  Users,
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

export function MetricsPanel() {
  const [days, setDays] = useState(7);

  const { data: summary } = useQuery({
    queryKey: ["metrics/summary"],
    queryFn: getMetricsSummary,
  });

  const { data: timelineData } = useQuery({
    queryKey: ["metrics/timeline", days],
    queryFn: () => getMetricsTimeline(days),
  });

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Métricas de calidad</h1>
        <p className="text-sm text-muted-foreground">
          Salud del agente: latencia, repeticiones, ventas atascadas y escalaciones.
        </p>
      </div>

      {/* Tarjetas resumen */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <MetricCard
          title="Trazas hoy"
          value={summary?.total_traces_today ?? 0}
          icon={Activity}
          subtitle={
            summary?.failed_traces_today ?? 0 > 0
              ? `${summary?.failed_traces_today ?? 0} con error`
              : "Sin fallos"
          }
        />
        <MetricCard
          title="Latencia media"
          value={
            summary?.avg_duration_ms != null ? `${summary.avg_duration_ms} ms` : "—"
          }
          icon={Clock}
          subtitle="Tiempo medio por traza"
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
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">Trazas por día</h2>
          <div className="flex items-center gap-2">
            <span className="text-xs text-muted-foreground">Últimos {days} días</span>
            <select
              value={days}
              onChange={(e) => setDays(parseInt(e.target.value))}
              className="text-xs border rounded-md px-2 py-1"
            >
              <option value={3}>3 días</option>
              <option value={7}>7 días</option>
              <option value={14}>14 días</option>
              <option value={30}>30 días</option>
            </select>
          </div>
        </div>

        <div className="mt-4 h-64 w-full">
          <ResponsiveContainer width="100%" height={300}>
            <LineChart data={timelineData?.timeline ?? []}>
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
                          : "—"
                        } media
                      </p>
                    </div>
                  );
                }}
              />
              <Line
                type="monotone"
                dataKey="total_traces"
                stroke="#3b82f6"
                strokeWidth={2}
                dot={{ fill: "#3b82f6", strokeWidth: 2 }}
              />
              <Line
                type="monotone"
                dataKey="failed_traces"
                stroke="#ef4444"
                strokeWidth={2}
                dot={{ fill: "#ef4444", strokeWidth: 2 }}
              />
            </LineChart>
          </ResponsiveContainer>
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
