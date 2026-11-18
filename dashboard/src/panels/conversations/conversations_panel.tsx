import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import type { ColumnDef } from "@tanstack/react-table";
import { AlertTriangle, MessageSquare, RefreshCw, Search } from "lucide-react";
import { getConversationsSummary, listConversations } from "@/lib/api";
import {
  formatDateTime,
  SALE_STATUS_LABELS,
  SALE_STATUS_VARIANTS,
} from "@/lib/format";
import type {
  ConversationListItem,
  ConversationStatus,
  CustomerKind,
} from "@/types/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { DataTable } from "@/components/data_table";
import { cn } from "@/lib/utils";
import { ConversationDetail } from "./conversation_detail";
import {
  CONVERSATION_STATUS_LABELS,
  CONVERSATION_STATUS_VARIANTS,
  CUSTOMER_KIND_LABELS,
} from "./labels";

const PAGE_SIZE = 50;

function SummaryCard({
  label,
  value,
  highlight = false,
}: {
  label: string;
  value: number | string;
  highlight?: boolean;
}) {
  return (
    <Card>
      <CardContent className="p-4">
        <p className="text-xs text-muted-foreground">{label}</p>
        <p className={cn("text-2xl font-semibold", highlight && "text-amber-500")}>
          {value}
        </p>
      </CardContent>
    </Card>
  );
}

const columns: ColumnDef<ConversationListItem, unknown>[] = [
  {
    id: "customer",
    header: "Cliente",
    accessorFn: (row) => row.customer.name ?? "",
    cell: ({ row }) => (
      <div>
        <p className="font-medium">{row.original.customer.name ?? "Sin nombre"}</p>
        <p className="text-xs text-muted-foreground">
          {row.original.customer.handle ?? "—"} ·{" "}
          {row.original.customer.phone_masked ?? "sin teléfono"}
        </p>
      </div>
    ),
  },
  {
    id: "kind",
    header: "Tipo",
    cell: ({ row }) => (
      <Badge variant={row.original.kind === "supplier" ? "secondary" : "outline"}>
        {CUSTOMER_KIND_LABELS[row.original.kind]}
      </Badge>
    ),
  },
  {
    accessorKey: "status",
    header: "Estado",
    cell: ({ row }) => (
      <Badge variant={CONVERSATION_STATUS_VARIANTS[row.original.status]}>
        {CONVERSATION_STATUS_LABELS[row.original.status]}
      </Badge>
    ),
  },
  {
    accessorKey: "stage",
    header: "Etapa",
    cell: ({ row }) => (
      <span className="text-xs">{row.original.stage ?? "—"}</span>
    ),
  },
  {
    id: "last_message",
    header: "Último mensaje",
    cell: ({ row }) => {
      const message = row.original.last_message;
      if (!message) return <span className="text-muted-foreground">—</span>;
      return (
        <div className="max-w-[320px]">
          <p className="truncate">{message.body ?? "—"}</p>
          <p className="text-xs text-muted-foreground">
            {message.direction === "INBOUND" ? "Cliente" : "Bot"} ·{" "}
            {formatDateTime(message.created_at)}
          </p>
        </div>
      );
    },
  },
  {
    id: "signals",
    header: "Señales",
    cell: ({ row }) => {
      const item = row.original;
      return (
        <div className="flex flex-wrap gap-1">
          {item.needs_attention && (
            <Badge variant="warning" className="gap-1">
              <AlertTriangle className="h-3 w-3" />
              Atención
            </Badge>
          )}
          {item.open_escalations > 0 && (
            <Badge variant="outline">
              {item.open_escalations} escalación
              {item.open_escalations === 1 ? "" : "es"}
            </Badge>
          )}
          {item.last_sale && (
            <Badge variant={SALE_STATUS_VARIANTS[item.last_sale.status]}>
              {SALE_STATUS_LABELS[item.last_sale.status]}
            </Badge>
          )}
        </div>
      );
    },
  },
  {
    id: "activity",
    header: "Actividad",
    cell: ({ row }) => (
      <span className="text-xs text-muted-foreground">
        {row.original.messages_count} mensajes · {row.original.turns_count} turnos
      </span>
    ),
  },
];

function ConversationList({ onOpen }: { onOpen: (id: number) => void }) {
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [kind, setKind] = useState<"" | CustomerKind>("");
  const [status, setStatus] = useState<"" | ConversationStatus>("");
  const [attention, setAttention] = useState<"" | "yes" | "no">("");
  const [sort, setSort] = useState("updated_at");
  const [offset, setOffset] = useState(0);

  const summary = useQuery({
    queryKey: ["conversations-summary"],
    queryFn: getConversationsSummary,
  });

  const { data, isLoading, isFetching, error, refetch } = useQuery({
    queryKey: ["conversations", kind, status, attention, search, sort, offset],
    queryFn: () =>
      listConversations({
        kind: kind || undefined,
        status: status || undefined,
        needs_attention: attention === "" ? undefined : attention === "yes",
        q: search || undefined,
        sort,
        limit: PAGE_SIZE,
        offset,
      }),
  });

  const total = data?.total ?? 0;
  const from = total === 0 ? 0 : offset + 1;
  const to = Math.min(offset + PAGE_SIZE, total);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Conversaciones</h1>
          <p className="text-sm text-muted-foreground">
            Qué está pasando en cada chat. Solo lectura: los teléfonos y las
            direcciones se muestran enmascarados.
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            refetch();
            summary.refetch();
          }}
          disabled={isFetching}
        >
          <RefreshCw className={isFetching ? "animate-spin" : undefined} />
          Actualizar
        </Button>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-6">
        <SummaryCard label="Total" value={summary.data?.total ?? 0} />
        <SummaryCard label="Abiertas" value={summary.data?.open ?? 0} />
        <SummaryCard label="Esperando TCP" value={summary.data?.waiting_tcp ?? 0} />
        <SummaryCard
          label="Requieren atención"
          value={summary.data?.needs_attention ?? 0}
          highlight
        />
        <SummaryCard label="Con venta" value={summary.data?.with_sale ?? 0} />
        <SummaryCard
          label="Clientes / Proveedores"
          value={`${summary.data?.customers ?? 0} / ${summary.data?.suppliers ?? 0}`}
        />
      </div>

      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          setOffset(0);
          setSearch(searchInput.trim());
        }}
      >
        <div className="relative">
          <Search className="pointer-events-none absolute top-2.5 left-2 h-4 w-4 text-muted-foreground" />
          <Input
            className="w-72 pl-8"
            placeholder="Nombre, teléfono, pedido o mensaje"
            value={searchInput}
            onChange={(event) => setSearchInput(event.target.value)}
          />
        </div>
        <Button type="submit" variant="outline" size="sm">
          Buscar
        </Button>

        <Select
          className="w-36"
          value={kind}
          onChange={(event) => {
            setKind(event.target.value as "" | CustomerKind);
            setOffset(0);
          }}
        >
          <option value="">Todos los tipos</option>
          <option value="customer">Clientes</option>
          <option value="supplier">Proveedores</option>
        </Select>

        <Select
          className="w-40"
          value={status}
          onChange={(event) => {
            setStatus(event.target.value as "" | ConversationStatus);
            setOffset(0);
          }}
        >
          <option value="">Todos los estados</option>
          <option value="ACTIVE">Activas</option>
          <option value="WAITING_TCP">Esperando TCP</option>
          <option value="CLOSED">Cerradas</option>
        </Select>

        <Select
          className="w-44"
          value={attention}
          onChange={(event) => {
            setAttention(event.target.value as "" | "yes" | "no");
            setOffset(0);
          }}
        >
          <option value="">Todas</option>
          <option value="yes">Solo atención</option>
          <option value="no">Sin atención</option>
        </Select>

        <Select
          className="w-44"
          value={sort}
          onChange={(event) => {
            setSort(event.target.value);
            setOffset(0);
          }}
        >
          <option value="updated_at">Actividad reciente</option>
          <option value="created_at">Más nuevas</option>
          <option value="last_message_at">Último mensaje</option>
          <option value="messages_count">Más mensajes</option>
          <option value="turns_count">Más turnos</option>
          <option value="questions_count">Más consultas</option>
        </Select>

        {search && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => {
              setSearchInput("");
              setSearch("");
              setOffset(0);
            }}
          >
            Limpiar búsqueda
          </Button>
        )}
      </form>

      {error && (
        <p className="text-sm text-destructive">
          No se pudieron cargar las conversaciones: {(error as Error).message}
        </p>
      )}

      <DataTable
        columns={columns}
        data={data?.items ?? []}
        onRowClick={(row) => onOpen(row.id)}
        empty={
          isLoading ? (
            "Cargando conversaciones…"
          ) : (
            <span className="inline-flex items-center gap-2">
              <MessageSquare className="h-4 w-4" />
              No hay conversaciones con estos filtros.
            </span>
          )
        }
      />

      <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
        <span className="text-muted-foreground">
          Mostrando {from}–{to} de {total}
        </span>
        <div className="flex gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
          >
            Anterior
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={offset + PAGE_SIZE >= total}
            onClick={() => setOffset(offset + PAGE_SIZE)}
          >
            Siguiente
          </Button>
        </div>
      </div>
    </div>
  );
}

export function ConversationsPanel() {
  const { conversationId } = useParams();
  const navigate = useNavigate();
  const id = conversationId ? Number(conversationId) : null;

  if (id !== null && Number.isFinite(id)) {
    return (
      <ConversationDetail
        conversationId={id}
        onBack={() => navigate("/conversations")}
      />
    );
  }
  return <ConversationList onOpen={(next) => navigate(`/conversations/${next}`)} />;
}
