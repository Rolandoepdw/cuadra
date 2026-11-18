import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  Clock,
  Cpu,
  ListOrdered,
  MessageSquare,
  Percent,
  ShieldAlert,
  UserCheck,
  Users,
} from "lucide-react";
import { getSummary } from "@/lib/api";
import { formatMs } from "@/lib/format";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

function Stat({
  title,
  value,
  icon: Icon,
}: {
  title: string;
  value: string | number;
  icon: React.ComponentType<{ className?: string }>;
}) {
  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between gap-2 pb-2">
        <CardTitle className="text-sm font-medium text-muted-foreground">{title}</CardTitle>
        <Icon className="h-4 w-4 text-muted-foreground" />
      </CardHeader>
      <CardContent>
        <div className="text-2xl font-semibold tabular-nums">{value}</div>
      </CardContent>
    </Card>
  );
}

export function GeneralPanel() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["summary"],
    queryFn: getSummary,
    refetchInterval: 15_000,
  });

  if (error) return <p className="text-destructive">Error: {(error as Error).message}</p>;

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">General</h1>
        <p className="text-sm text-muted-foreground">Resumen del día (se actualiza solo).</p>
      </div>

      {isLoading ? (
        <p className="text-muted-foreground">Cargando…</p>
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <Stat title="Mensajes hoy" value={data?.messages_today ?? 0} icon={MessageSquare} />
          <Stat title="Conversaciones activas" value={data?.active_conversations ?? 0} icon={Users} />
          <Stat title="Clientes activos (7 d)" value={data?.customers_active_7d ?? 0} icon={UserCheck} />
          <Stat
            title="Conversión de clientes"
            value={`${((data?.customers_conversion ?? 0) * 100).toFixed(0)}%`}
            icon={Percent}
          />
          <Stat title="Clientes en riesgo" value={data?.customers_at_risk ?? 0} icon={ShieldAlert} />
          <Stat title="Escalados abiertos" value={data?.escalations_open ?? 0} icon={AlertTriangle} />
          <Stat title="Latencia media LLM" value={formatMs(data?.avg_llm_ms)} icon={Cpu} />
          <Stat title="Latencia media STT" value={formatMs(data?.avg_stt_ms)} icon={Clock} />
          <Stat title="Cola de trabajos" value={data?.queue_size ?? 0} icon={ListOrdered} />
        </div>
      )}
    </div>
  );
}
