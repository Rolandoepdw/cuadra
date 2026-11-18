import { useEffect, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowLeft,
  MessageSquare,
  RefreshCw,
  ShieldAlert,
  X,
} from "lucide-react";
import {
  closeConversation,
  getConversation,
  listConversationMessages,
} from "@/lib/api";
import {
  formatDateTime,
  formatMoney,
  formatMs,
  SALE_STATUS_LABELS,
  SALE_STATUS_VARIANTS,
} from "@/lib/format";
import type {
  ConversationCustomerDetail,
  ConversationMessage,
  ConversationTurn,
} from "@/types/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/confirm_dialog";
import { MessageBubble } from "./message_bubble";
import {
  CONVERSATION_STATUS_LABELS,
  CONVERSATION_STATUS_VARIANTS,
  CUSTOMER_KIND_LABELS,
} from "./labels";

const ESCALATION_STATUS_VARIANTS: Record<
  string,
  "success" | "warning" | "secondary" | "destructive" | "outline"
> = {
  PENDING: "warning",
  ANSWERED: "secondary",
  RESOLVED: "success",
  TIMEOUT: "destructive",
  CANCELLED: "outline",
};

function Field({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex justify-between gap-3">
      <span className="shrink-0 text-muted-foreground">{label}</span>
      <span className="text-right break-words">{value ?? "—"}</span>
    </div>
  );
}

function CustomerCard({ customer }: { customer: ConversationCustomerDetail }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Cliente</CardTitle>
      </CardHeader>
      <CardContent className="space-y-1.5 text-sm">
        <Field label="Nombre" value={customer.name} />
        <Field label="Handle" value={customer.handle} />
        <Field
          label="Teléfono"
          value={
            <span>
              {customer.phone_masked ?? "—"}{" "}
              <span className="text-xs text-muted-foreground">(enmascarado)</span>
            </span>
          }
        />
        <Field label="Zona habitual" value={customer.usual_zone} />
        <Field label="Dirección" value={customer.usual_address_masked} />
        <Field label="Notas" value={customer.notes} />
        <Field label="Cliente desde" value={formatDateTime(customer.created_at)} />
      </CardContent>
    </Card>
  );
}

function ContextCard({ context }: { context: Record<string, unknown> }) {
  const entries = Object.entries(context).filter(([key]) => key !== "summary");
  const summary = typeof context.summary === "string" ? context.summary : null;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Contexto</CardTitle>
      </CardHeader>
      <CardContent className="space-y-1.5 text-sm">
        {summary && (
          <p className="mb-2 rounded-md bg-muted/40 p-2 text-xs whitespace-pre-wrap">
            {summary}
          </p>
        )}
        {entries.length === 0 ? (
          <p className="text-muted-foreground">Sin datos de contexto.</p>
        ) : (
          entries.map(([key, value]) => (
            <Field
              key={key}
              label={key}
              value={
                typeof value === "string" || typeof value === "number"
                  ? String(value)
                  : JSON.stringify(value)
              }
            />
          ))
        )}
      </CardContent>
    </Card>
  );
}

function TurnsCard({ turns }: { turns: ConversationTurn[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Turnos procesados</CardTitle>
      </CardHeader>
      <CardContent>
        {turns.length === 0 ? (
          <p className="text-sm text-muted-foreground">Sin turnos.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-muted/50 text-left text-xs">
                <tr>
                  <th className="px-2 py-1.5 font-medium">#</th>
                  <th className="px-2 py-1.5 font-medium">Estado</th>
                  <th className="px-2 py-1.5 font-medium">Intención</th>
                  <th className="px-2 py-1.5 font-medium">Producto</th>
                  <th className="px-2 py-1.5 font-medium">Etapa</th>
                  <th className="px-2 py-1.5 font-medium">Generador</th>
                  <th className="px-2 py-1.5 font-medium">Latencia</th>
                  <th className="px-2 py-1.5 font-medium">Resultado</th>
                </tr>
              </thead>
              <tbody>
                {turns.map((turn) => (
                  <tr key={turn.id} className="border-t">
                    <td className="px-2 py-1.5">{turn.id}</td>
                    <td className="px-2 py-1.5">{turn.status}</td>
                    <td className="px-2 py-1.5">{turn.intent ?? "—"}</td>
                    <td className="px-2 py-1.5">{turn.product_name ?? "—"}</td>
                    <td className="px-2 py-1.5">
                      {turn.stage ?? "—"}
                      {turn.escalated && (
                        <Badge variant="warning" className="ml-1">
                          escalado
                        </Badge>
                      )}
                    </td>
                    <td className="px-2 py-1.5">{turn.generator ?? "—"}</td>
                    <td className="px-2 py-1.5">{formatMs(turn.latency_ms)}</td>
                    <td className="px-2 py-1.5">
                      {turn.outcome === "error" ? (
                        <Badge variant="destructive">error</Badge>
                      ) : (
                        <Badge variant="success">ok</Badge>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export function ConversationDetail({
  conversationId,
  onBack,
}: {
  conversationId: number;
  onBack: () => void;
}) {
  const queryClient = useQueryClient();
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [older, setOlder] = useState<ConversationMessage[]>([]);
  const [olderHasMore, setOlderHasMore] = useState<boolean | null>(null);
  const [loadingOlder, setLoadingOlder] = useState(false);

  const { data, isLoading, isFetching, error, refetch } = useQuery({
    queryKey: ["conversation", conversationId],
    queryFn: () => getConversation(conversationId),
  });

  useEffect(() => {
    setOlder([]);
    setOlderHasMore(null);
  }, [conversationId]);

  const close = useMutation({
    mutationFn: () => closeConversation(conversationId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["conversation", conversationId] });
      queryClient.invalidateQueries({ queryKey: ["conversations"] });
      queryClient.invalidateQueries({ queryKey: ["conversations-summary"] });
      setConfirmOpen(false);
    },
  });

  async function loadOlder() {
    const first = older[0] ?? data?.messages[0];
    if (!first) return;
    setLoadingOlder(true);
    try {
      const page = await listConversationMessages(conversationId, {
        before_id: first.id,
        limit: 50,
      });
      setOlder((previous) => [...page.items, ...previous]);
      setOlderHasMore(page.has_more);
    } finally {
      setLoadingOlder(false);
    }
  }

  if (isLoading) {
    return <p className="text-sm text-muted-foreground">Cargando conversación…</p>;
  }
  if (error || !data) {
    return (
      <div className="space-y-3">
        <Button variant="outline" size="sm" onClick={onBack}>
          <ArrowLeft />
          Volver
        </Button>
        <p className="text-sm text-destructive">
          No se pudo cargar la conversación: {(error as Error)?.message ?? "error"}
        </p>
      </div>
    );
  }

  const { conversation, customer } = data;
  const messages = [...older, ...data.messages];
  const hasMore = olderHasMore ?? data.messages_has_more;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex items-start gap-3">
          <Button variant="outline" size="sm" onClick={onBack}>
            <ArrowLeft />
            Volver
          </Button>
          <div>
            <h2 className="text-lg font-semibold">
              {customer.name ?? "Cliente sin nombre"}
            </h2>
            <p className="text-xs text-muted-foreground">
              {customer.handle ?? "sin handle"} · {conversation.channel} ·{" "}
              {conversation.identity}
            </p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={conversation.kind === "supplier" ? "secondary" : "outline"}>
            {CUSTOMER_KIND_LABELS[conversation.kind]}
          </Badge>
          <Badge variant={CONVERSATION_STATUS_VARIANTS[conversation.status]}>
            {CONVERSATION_STATUS_LABELS[conversation.status]}
          </Badge>
          {conversation.needs_attention && (
            <Badge variant="warning" className="gap-1">
              <AlertTriangle className="h-3 w-3" />
              Requiere atención
            </Badge>
          )}
          {conversation.status !== "CLOSED" && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => setConfirmOpen(true)}
              disabled={close.isPending}
            >
              <X />
              Cerrar conversación
            </Button>
          )}
        </div>
      </div>

      {close.isError && (
        <p className="text-sm text-destructive">
          No se pudo cerrar: {(close.error as Error).message}
        </p>
      )}

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_340px]">
        <Card>
          <CardHeader className="flex-row items-center justify-between">
            <CardTitle className="flex items-center gap-2">
              <MessageSquare className="h-4 w-4" />
              Conversación
            </CardTitle>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => refetch()}
              disabled={isFetching}
            >
              <RefreshCw className={isFetching ? "animate-spin" : undefined} />
              Actualizar
            </Button>
          </CardHeader>
          <CardContent className="space-y-3">
            {hasMore && (
              <div className="text-center">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={loadOlder}
                  disabled={loadingOlder}
                >
                  {loadingOlder ? "Cargando…" : "Cargar mensajes anteriores"}
                </Button>
              </div>
            )}
            {messages.length === 0 ? (
              <p className="text-sm text-muted-foreground">Sin mensajes.</p>
            ) : (
              messages.map((message) => (
                <MessageBubble key={message.id} message={message} />
              ))
            )}
          </CardContent>
        </Card>

        <div className="space-y-4">
          <CustomerCard customer={customer} />
          <ContextCard context={data.context} />

          <Card>
            <CardHeader>
              <CardTitle>Datos recordados</CardTitle>
            </CardHeader>
            <CardContent className="space-y-1.5 text-sm">
              {data.facts.length === 0 ? (
                <p className="text-muted-foreground">Sin datos recordados.</p>
              ) : (
                data.facts.map((fact) => (
                  <Field
                    key={fact.fact_key}
                    label={fact.fact_key}
                    value={
                      <span>
                        {fact.fact_value}{" "}
                        <span className="text-xs text-muted-foreground">
                          {Math.round(fact.confidence * 100)}%
                        </span>
                      </span>
                    }
                  />
                ))
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Ventas</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2 text-sm">
              {data.sales.length === 0 ? (
                <p className="text-muted-foreground">Sin ventas.</p>
              ) : (
                data.sales.map((sale) => (
                  <div key={sale.id} className="rounded-md border p-2">
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-medium">
                        {sale.order_code ?? `Venta ${sale.id}`}
                      </span>
                      <Badge variant={SALE_STATUS_VARIANTS[sale.status]}>
                        {SALE_STATUS_LABELS[sale.status]}
                      </Badge>
                    </div>
                    <p className="text-xs text-muted-foreground">
                      {sale.product_name ?? "Producto"} · {sale.quantity} ×{" "}
                      {formatMoney(sale.amount, sale.currency)}
                    </p>
                  </div>
                ))
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Escalaciones</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2 text-sm">
              {data.escalations.length === 0 ? (
                <p className="text-muted-foreground">Sin escalaciones.</p>
              ) : (
                data.escalations.map((escalation) => (
                  <div key={escalation.id} className="rounded-md border p-2">
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-xs text-muted-foreground">
                        {escalation.product_name ?? "Producto"}
                      </span>
                      <Badge
                        variant={
                          ESCALATION_STATUS_VARIANTS[escalation.status] ?? "outline"
                        }
                      >
                        {escalation.status}
                      </Badge>
                    </div>
                    <p className="mt-1">{escalation.question}</p>
                    {escalation.tcp_answer && (
                      <p className="mt-1 text-xs text-muted-foreground">
                        Respuesta: {escalation.tcp_answer}
                      </p>
                    )}
                  </div>
                ))
              )}
            </CardContent>
          </Card>
        </div>
      </div>

      <TurnsCard turns={data.turns} />

      <Card>
        <CardHeader>
          <CardTitle>Trazas recientes</CardTitle>
        </CardHeader>
        <CardContent className="space-y-1 text-sm">
          {data.traces.length === 0 ? (
            <p className="text-muted-foreground">Sin trazas.</p>
          ) : (
            data.traces.map((trace) => (
              <div key={trace.id} className="flex items-center justify-between gap-2">
                <span className="flex items-center gap-2">
                  <Badge variant={trace.ok ? "success" : "destructive"}>
                    {trace.ok ? "ok" : "error"}
                  </Badge>
                  <span className="font-mono text-xs">{trace.method}</span>
                </span>
                <span className="text-xs text-muted-foreground">
                  {formatMs(trace.duration_ms)} · {formatDateTime(trace.timestamp)}
                </span>
              </div>
            ))
          )}
        </CardContent>
      </Card>

      <p className="flex items-center gap-2 text-xs text-muted-foreground">
        <ShieldAlert className="h-3 w-3" />
        Teléfonos y direcciones se muestran enmascarados. La transcripción conserva
        el texto original para poder atender al cliente.
      </p>

      <ConfirmDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title="Cerrar conversación"
        description="Se cancelan los turnos pendientes y el bot deja de responder en este hilo. El historial se conserva."
        confirmLabel="Cerrar conversación"
        destructive
        loading={close.isPending}
        onConfirm={() => close.mutate()}
      />
    </div>
  );
}
