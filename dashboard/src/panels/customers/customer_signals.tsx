import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, ShieldAlert, X } from "lucide-react";
import { confirmCustomerSignal, dismissCustomerSignal } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import type { CustomerSignal } from "@/types/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  SIGNAL_KIND_LABELS,
  SIGNAL_SEVERITY_LABELS,
  SIGNAL_SEVERITY_VARIANTS,
} from "./labels";

const STATUS_LABELS: Record<string, string> = {
  OPEN: "Por revisar",
  CONFIRMED: "Confirmada",
  DISMISSED: "Descartada",
};

export function CustomerSignals({
  customerId,
  signals,
}: {
  customerId: number;
  signals: CustomerSignal[];
}) {
  const queryClient = useQueryClient();
  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ["customer", customerId] });
    queryClient.invalidateQueries({ queryKey: ["customers"] });
    queryClient.invalidateQueries({ queryKey: ["customers-summary"] });
  };

  const confirm = useMutation({
    mutationFn: (signalId: number) => confirmCustomerSignal(customerId, signalId),
    onSuccess: refresh,
  });
  const dismiss = useMutation({
    mutationFn: (signalId: number) => dismissCustomerSignal(customerId, signalId),
    onSuccess: refresh,
  });

  if (!signals.length) {
    return (
      <p className="text-sm text-muted-foreground">
        Sin señales. Nada pendiente de revisar.
      </p>
    );
  }

  return (
    <div className="space-y-3">
      {signals.map((signal) => (
        <div
          key={signal.id}
          className="rounded-lg border p-3 text-sm"
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex flex-wrap items-center gap-2">
              <ShieldAlert className="h-4 w-4 text-muted-foreground" />
              <span className="font-medium">
                {SIGNAL_KIND_LABELS[signal.kind] ?? signal.kind}
              </span>
              <Badge variant={SIGNAL_SEVERITY_VARIANTS[signal.severity] ?? "outline"}>
                {SIGNAL_SEVERITY_LABELS[signal.severity] ?? signal.severity}
              </Badge>
              <Badge variant={signal.status === "OPEN" ? "warning" : "secondary"}>
                {STATUS_LABELS[signal.status] ?? signal.status}
              </Badge>
              {signal.count > 1 && (
                <span className="text-xs text-muted-foreground">
                  {signal.count} veces
                </span>
              )}
            </div>
            {signal.status === "OPEN" && (
              <div className="flex gap-2">
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => confirm.mutate(signal.id)}
                  disabled={confirm.isPending || dismiss.isPending}
                >
                  <Check className="h-3 w-3" />
                  Confirmar
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => dismiss.mutate(signal.id)}
                  disabled={confirm.isPending || dismiss.isPending}
                >
                  <X className="h-3 w-3" />
                  Descartar
                </Button>
              </div>
            )}
          </div>
          <p className="mt-1 text-xs text-muted-foreground">
            Visto {formatDateTime(signal.first_seen_at)}
            {signal.resolved_at
              ? ` · resuelto ${formatDateTime(signal.resolved_at)}`
              : ""}
          </p>
          {Object.keys(signal.evidence ?? {}).length > 0 && (
            <pre className="mt-2 overflow-x-auto rounded bg-muted/50 p-2 text-xs">
              {JSON.stringify(signal.evidence, null, 2)}
            </pre>
          )}
        </div>
      ))}
    </div>
  );
}
