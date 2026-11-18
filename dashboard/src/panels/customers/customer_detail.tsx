import { useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import {
  ArrowLeft,
  Ban,
  Eye,
  GitMerge,
  RefreshCw,
  ShieldCheck,
  Tag,
  Unlock,
} from "lucide-react";
import {
  addCustomerNote,
  addCustomerTag,
  blockCustomer,
  getCustomer,
  removeCustomerTag,
  revealCustomerContact,
  unblockCustomer,
} from "@/lib/api";
import { formatDateTime, formatMoney } from "@/lib/format";
import type { CustomerReveal } from "@/types/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { CustomerMergeDialog } from "./customer_merge_dialog";
import { CustomerSignals } from "./customer_signals";
import {
  CUSTOMER_RISK_LABELS,
  CUSTOMER_RISK_VARIANTS,
  CUSTOMER_SEGMENT_LABELS,
  CUSTOMER_SEGMENT_VARIANTS,
  CUSTOMER_STATUS_LABELS,
  CUSTOMER_STATUS_VARIANTS,
} from "./labels";

function Field({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex justify-between gap-3">
      <span className="shrink-0 text-muted-foreground">{label}</span>
      <span className="text-right break-words">{value ?? "—"}</span>
    </div>
  );
}

function Metric({
  label,
  value,
  hint,
}: {
  label: string;
  value: ReactNode;
  hint?: string;
}) {
  return (
    <div className="rounded-md border p-3">
      <p className="text-xs text-muted-foreground" title={hint}>
        {label}
      </p>
      <p className="text-lg font-semibold">{value}</p>
    </div>
  );
}

function money(usd: number, cup: number): string {
  const parts: string[] = [];
  if (usd) parts.push(formatMoney(usd, "USD"));
  if (cup) parts.push(formatMoney(cup, "CUP"));
  return parts.length ? parts.join(" · ") : "—";
}

export function CustomerDetail({
  customerId,
  onBack,
  onMerged,
}: {
  customerId: number;
  onBack: () => void;
  onMerged: (intoId: number) => void;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [revealed, setRevealed] = useState<CustomerReveal | null>(null);
  const [tagInput, setTagInput] = useState("");
  const [noteInput, setNoteInput] = useState("");
  const [mergeOpen, setMergeOpen] = useState(false);

  const { data, isLoading, isFetching, error, refetch } = useQuery({
    queryKey: ["customer", customerId],
    queryFn: () => getCustomer(customerId),
  });

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ["customer", customerId] });
    queryClient.invalidateQueries({ queryKey: ["customers"] });
    queryClient.invalidateQueries({ queryKey: ["customers-summary"] });
  };

  const reveal = useMutation({
    mutationFn: () => revealCustomerContact(customerId),
    onSuccess: setRevealed,
  });
  const toggleBlock = useMutation({
    mutationFn: (blocked: boolean) =>
      blocked ? unblockCustomer(customerId) : blockCustomer(customerId),
    onSuccess: refresh,
  });
  const addTag = useMutation({
    mutationFn: (tag: string) => addCustomerTag(customerId, tag),
    onSuccess: () => {
      setTagInput("");
      refresh();
    },
  });
  const removeTag = useMutation({
    mutationFn: (tag: string) => removeCustomerTag(customerId, tag),
    onSuccess: refresh,
  });
  const addNote = useMutation({
    mutationFn: (body: string) => addCustomerNote(customerId, body),
    onSuccess: () => {
      setNoteInput("");
      refresh();
    },
  });

  if (isLoading) {
    return <p className="text-sm text-muted-foreground">Cargando cliente…</p>;
  }
  if (error || !data) {
    return (
      <div className="space-y-3">
        <Button variant="outline" size="sm" onClick={onBack}>
          <ArrowLeft />
          Volver
        </Button>
        <p className="text-sm text-destructive">
          No se pudo cargar el cliente: {(error as Error | null)?.message ?? "no encontrado"}
        </p>
      </div>
    );
  }

  const customer = data.customer;
  const metrics = data.metrics;
  const blocked = customer.status === "BLOCKED" || customer.status === "SPAM";

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
              {customer.display_name ?? customer.name ?? "Cliente sin nombre"}
            </h2>
            <p className="text-xs text-muted-foreground">
              #{customer.id} ·{" "}
              {customer.channels.length
                ? customer.channels
                    .map((channel) => `${channel.channel}: ${channel.handle ?? "—"}`)
                    .join(" · ")
                : "sin canal"}{" "}
              · {customer.zone ?? "sin zona"}
            </p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={CUSTOMER_SEGMENT_VARIANTS[customer.segment]}>
            {CUSTOMER_SEGMENT_LABELS[customer.segment]}
          </Badge>
          <Badge variant={CUSTOMER_RISK_VARIANTS[customer.risk]}>
            Riesgo {CUSTOMER_RISK_LABELS[customer.risk]}
            {customer.risk_score > 0 ? ` (${customer.risk_score})` : ""}
          </Badge>
          <Badge variant={CUSTOMER_STATUS_VARIANTS[customer.status]}>
            {CUSTOMER_STATUS_LABELS[customer.status]}
          </Badge>
          <Button
            variant="outline"
            size="sm"
            onClick={() => reveal.mutate()}
            disabled={reveal.isPending}
          >
            <Eye />
            {revealed ? "Revelado" : "Revelar PII"}
          </Button>
          <Button
            variant={blocked ? "outline" : "destructive"}
            size="sm"
            onClick={() => toggleBlock.mutate(blocked)}
            disabled={toggleBlock.isPending}
          >
            {blocked ? <Unlock /> : <Ban />}
            {blocked ? "Desbloquear" : "Bloquear"}
          </Button>
          <Button variant="outline" size="sm" onClick={() => setMergeOpen(true)}>
            <GitMerge />
            Fusionar
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => refetch()}
            disabled={isFetching}
          >
            <RefreshCw className={isFetching ? "animate-spin" : undefined} />
          </Button>
        </div>
      </div>

      {reveal.isError && (
        <p className="text-sm text-destructive">
          No se pudo revelar: {(reveal.error as Error).message}
        </p>
      )}

      <div className="grid gap-4 lg:grid-cols-3">
        <Card>
          <CardHeader>
            <CardTitle>Perfil</CardTitle>
          </CardHeader>
          <CardContent className="space-y-1.5 text-sm">
            <Field label="Nombre" value={customer.name} />
            <Field label="Nombre de entrega" value={customer.display_name} />
            <Field
              label="Teléfono"
              value={
                <span className="font-mono text-xs">
                  {revealed?.phone ?? customer.phone_masked ?? "—"}
                </span>
              }
            />
            <Field
              label="Dirección"
              value={
                <span className="text-xs">
                  {revealed?.address ?? customer.address_masked ?? "—"}
                </span>
              }
            />
            <Field label="Zona habitual" value={customer.zone} />
            <Field
              label="Primera actividad"
              value={formatDateTime(metrics.first_seen_at)}
            />
            <Field
              label="Última actividad"
              value={formatDateTime(metrics.last_seen_at)}
            />
            <Field
              label="Última compra"
              value={formatDateTime(metrics.last_purchase_at)}
            />
            {revealed && (
              <div className="mt-2 rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-xs">
                <p className="flex items-center gap-1 font-medium">
                  <ShieldCheck className="h-3 w-3" />
                  PII revelada (evento auditado)
                </p>
                {revealed.contacts.map((contact) => (
                  <p key={contact.id} className="font-mono">
                    {contact.kind}: {contact.value} ({contact.source})
                  </p>
                ))}
              </div>
            )}
          </CardContent>
        </Card>

        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>Esfuerzo y resultado</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div>
              <p className="mb-2 text-xs font-medium text-muted-foreground">
                Esfuerzo (lo que consumió)
              </p>
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
                <Metric label="Turnos" value={metrics.turns_total} />
                <Metric label="Mensajes in/out" value={`${metrics.messages_in}/${metrics.messages_out}`} />
                <Metric label="Preguntas" value={metrics.questions_total} />
                <Metric label="Escalaciones" value={metrics.escalations_total} />
                <Metric
                  label="Cómputo"
                  value={`${metrics.compute_s.toFixed(1)} s`}
                  hint="Suma de tiempo de intención y redacción"
                />
              </div>
              <p className="mt-1 text-xs text-muted-foreground">
                Puntaje de esfuerzo: {metrics.effort_score} (turnos + 2×escalaciones + mensajes/2)
              </p>
            </div>
            <div>
              <p className="mb-2 text-xs font-medium text-muted-foreground">
                Resultado (lo que produjo)
              </p>
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
                <Metric label="Ventas liquidadas" value={metrics.sales_settled} />
                <Metric label="Ventas creadas" value={metrics.sales_created} />
                <Metric label="Conversión" value={`${(metrics.conversion * 100).toFixed(0)}%`} />
                <Metric label="Ingresos" value={money(metrics.revenue_usd, metrics.revenue_cup)} />
                <Metric label="Comisión cobrada" value={money(metrics.commission_usd, metrics.commission_cup)} />
              </div>
            </div>
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Señales</CardTitle>
          </CardHeader>
          <CardContent>
            <CustomerSignals customerId={customerId} signals={data.signals} />
          </CardContent>
        </Card>

        <div className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>Etiquetas</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              <div className="flex flex-wrap gap-1">
                {data.tags.length ? (
                  data.tags.map((tag) => (
                    <Badge key={tag} variant="outline" className="gap-1">
                      <Tag className="h-3 w-3" />
                      {tag}
                      <button
                        type="button"
                        className="ml-1 text-muted-foreground hover:text-destructive"
                        onClick={() => removeTag.mutate(tag)}
                        disabled={removeTag.isPending}
                        aria-label={`Quitar ${tag}`}
                      >
                        ×
                      </button>
                    </Badge>
                  ))
                ) : (
                  <span className="text-sm text-muted-foreground">Sin etiquetas.</span>
                )}
              </div>
              <form
                className="flex gap-2"
                onSubmit={(event) => {
                  event.preventDefault();
                  const tag = tagInput.trim();
                  if (tag) addTag.mutate(tag);
                }}
              >
                <Input
                  value={tagInput}
                  maxLength={40}
                  placeholder="Nueva etiqueta"
                  onChange={(event) => setTagInput(event.target.value)}
                />
                <Button type="submit" size="sm" disabled={addTag.isPending}>
                  Añadir
                </Button>
              </form>
              {addTag.isError && (
                <p className="text-xs text-destructive">
                  {(addTag.error as Error).message}
                </p>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Notas</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              {data.notes.length ? (
                data.notes.map((note) => (
                  <div key={note.id} className="rounded-md border p-2 text-sm">
                    <p className="whitespace-pre-wrap">{note.body}</p>
                    <p className="text-xs text-muted-foreground">
                      {note.author ?? "operador"} · {formatDateTime(note.created_at)}
                    </p>
                  </div>
                ))
              ) : (
                <p className="text-sm text-muted-foreground">Sin notas.</p>
              )}
              <form
                className="space-y-2"
                onSubmit={(event) => {
                  event.preventDefault();
                  const body = noteInput.trim();
                  if (body) addNote.mutate(body);
                }}
              >
                <Textarea
                  rows={2}
                  maxLength={2000}
                  placeholder="Nota interna sobre este cliente"
                  value={noteInput}
                  onChange={(event) => setNoteInput(event.target.value)}
                />
                <Button type="submit" size="sm" disabled={addNote.isPending}>
                  Añadir nota
                </Button>
              </form>
            </CardContent>
          </Card>
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Conversaciones</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {data.conversations.length ? (
              data.conversations.map((conversation) => (
                <button
                  key={conversation.id}
                  type="button"
                  className="flex w-full items-center justify-between gap-2 rounded-md border p-2 text-left text-sm hover:bg-muted/40"
                  onClick={() => navigate(`/conversations/${conversation.id}`)}
                >
                  <span>
                    #{conversation.id} · {conversation.handle ?? conversation.identity} ·{" "}
                    {conversation.status}
                  </span>
                  <span className="text-xs text-muted-foreground">
                    {conversation.messages_count} mensajes ·{" "}
                    {formatDateTime(conversation.last_message_at)}
                  </span>
                </button>
              ))
            ) : (
              <p className="text-sm text-muted-foreground">Sin conversaciones.</p>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Ventas</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {data.sales.length ? (
              data.sales.map((sale) => (
                <div
                  key={sale.id}
                  className="flex items-center justify-between gap-2 rounded-md border p-2 text-sm"
                >
                  <span>
                    {sale.order_code ?? `#${sale.id}`} · {sale.product_name ?? "—"}
                  </span>
                  <span className="text-xs text-muted-foreground">
                    {formatMoney(sale.amount, sale.currency)} · {sale.status}
                  </span>
                </div>
              ))
            ) : (
              <p className="text-sm text-muted-foreground">Sin ventas.</p>
            )}
          </CardContent>
        </Card>
      </div>

      <CustomerMergeDialog
        open={mergeOpen}
        onOpenChange={setMergeOpen}
        customerId={customerId}
        onMerged={onMerged}
      />
    </div>
  );
}
