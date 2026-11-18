import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import type { ColumnDef } from "@tanstack/react-table";
import { Contact, RefreshCw, Search } from "lucide-react";
import {
  getCustomersSummary,
  getCustomerSettings,
  listCustomers,
  updateCustomerSettings,
} from "@/lib/api";
import { formatDateTime, formatMoney } from "@/lib/format";
import type {
  CustomerListItem,
  CustomerRisk,
  CustomerSegment,
} from "@/types/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { DataTable } from "@/components/data_table";
import { cn } from "@/lib/utils";
import { CustomerDetail } from "./customer_detail";
import {
  CUSTOMER_RISK_LABELS,
  CUSTOMER_RISK_VARIANTS,
  CUSTOMER_SEGMENT_LABELS,
  CUSTOMER_SEGMENT_VARIANTS,
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

function money(usd: number, cup: number): string {
  const parts: string[] = [];
  if (usd) parts.push(formatMoney(usd, "USD"));
  if (cup) parts.push(formatMoney(cup, "CUP"));
  return parts.length ? parts.join(" · ") : "—";
}

function RetentionPolicy() {
  const queryClient = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["customers-settings"],
    queryFn: getCustomerSettings,
  });
  const [days, setDays] = useState("");

  useEffect(() => {
    if (data) setDays(String(data.message_retention_days));
  }, [data?.message_retention_days]);

  const save = useMutation({
    mutationFn: updateCustomerSettings,
    onSuccess: (next) => {
      setDays(String(next.message_retention_days));
      queryClient.invalidateQueries({ queryKey: ["customers-settings"] });
    },
  });

  const parsed = Number(days);
  const valid =
    data !== undefined &&
    Number.isInteger(parsed) &&
    parsed >= data.message_retention_min &&
    parsed <= data.message_retention_max;

  return (
    <form
      className="flex flex-wrap items-end gap-3 rounded-md border p-3 text-sm"
      onSubmit={(event) => {
        event.preventDefault();
        if (valid) save.mutate(parsed);
      }}
    >
      <div>
        <p className="text-xs text-muted-foreground">Retención de mensajes</p>
        <div className="mt-1 flex items-center gap-2">
          <Input
            type="number"
            className="w-24"
            min={data?.message_retention_min ?? 1}
            max={data?.message_retention_max ?? 3650}
            value={days}
            disabled={isLoading}
            onChange={(event) => setDays(event.target.value)}
          />
          <span>días</span>
          <Button type="submit" size="sm" disabled={!valid || save.isPending}>
            {save.isPending ? "Guardando…" : "Guardar"}
          </Button>
        </div>
      </div>
      <p className="max-w-md text-xs text-muted-foreground">
        Por defecto 180 días (6 meses). Solo se borran los mensajes; el perfil,
        las ventas y las comisiones se conservan.
      </p>
      {save.isError && (
        <p className="text-xs text-destructive">
          {(save.error as Error).message}
        </p>
      )}
    </form>
  );
}

const columns: ColumnDef<CustomerListItem, unknown>[] = [
  {
    id: "customer",
    header: "Cliente",
    accessorFn: (row) => row.display_name ?? row.name ?? "",
    cell: ({ row }) => {
      const item = row.original;
      return (
        <div>
          <p className="font-medium">
            {item.display_name ?? item.name ?? "Sin nombre"}
          </p>
          <p className="text-xs text-muted-foreground">
            {item.channels.length
              ? item.channels
                  .map((channel) => `${channel.channel}: ${channel.handle ?? "—"}`)
                  .join(" · ")
              : "sin canal"}
          </p>
        </div>
      );
    },
  },
  {
    id: "phone",
    header: "Teléfono",
    cell: ({ row }) => (
      <span className="font-mono text-xs">
        {row.original.phone_masked ?? "—"}
      </span>
    ),
  },
  {
    id: "zone",
    header: "Zona",
    cell: ({ row }) => (
      <span className="text-xs">{row.original.zone ?? "—"}</span>
    ),
  },
  {
    accessorKey: "conversations_total",
    header: "Chats",
    cell: ({ row }) => (
      <span className="text-xs">
        {row.original.conversations_total}
        {row.original.conversations_open > 0 && (
          <span className="text-muted-foreground">
            {" "}
            ({row.original.conversations_open} abiertos)
          </span>
        )}
      </span>
    ),
  },
  {
    accessorKey: "questions_total",
    header: "Preguntas",
  },
  {
    id: "sales",
    header: "Ventas",
    cell: ({ row }) => (
      <span className="text-xs">
        {row.original.sales_settled} liquidadas / {row.original.sales_created}
      </span>
    ),
  },
  {
    id: "revenue",
    header: "Ingresos",
    cell: ({ row }) => (
      <span className="text-xs">
        {money(row.original.revenue_usd, row.original.revenue_cup)}
      </span>
    ),
  },
  {
    id: "conversion",
    header: "Conversión",
    cell: ({ row }) => (
      <span className="text-xs">
        {(row.original.conversion * 100).toFixed(0)}%
      </span>
    ),
  },
  {
    id: "segment",
    header: "Segmento",
    cell: ({ row }) => (
      <Badge variant={CUSTOMER_SEGMENT_VARIANTS[row.original.segment]}>
        {CUSTOMER_SEGMENT_LABELS[row.original.segment]}
      </Badge>
    ),
  },
  {
    id: "risk",
    header: "Riesgo",
    cell: ({ row }) => (
      <Badge variant={CUSTOMER_RISK_VARIANTS[row.original.risk]}>
        {CUSTOMER_RISK_LABELS[row.original.risk]}
        {row.original.risk_score > 0 ? ` (${row.original.risk_score})` : ""}
      </Badge>
    ),
  },
  {
    id: "tags",
    header: "Etiquetas",
    cell: ({ row }) => (
      <div className="flex max-w-[180px] flex-wrap gap-1">
        {row.original.tags.length
          ? row.original.tags.map((tag) => (
              <Badge key={tag} variant="outline">
                {tag}
              </Badge>
            ))
          : "—"}
      </div>
    ),
  },
  {
    id: "last_seen",
    header: "Última actividad",
    cell: ({ row }) => (
      <span className="text-xs text-muted-foreground">
        {formatDateTime(row.original.last_seen_at)}
      </span>
    ),
  },
];

function CustomerList({ onOpen }: { onOpen: (id: number) => void }) {
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [segment, setSegment] = useState<"" | CustomerSegment>("");
  const [risk, setRisk] = useState<"" | CustomerRisk>("");
  const [sales, setSales] = useState<"" | "yes" | "no">("");
  const [sort, setSort] = useState("last_seen_at");
  const [offset, setOffset] = useState(0);

  const summary = useQuery({
    queryKey: ["customers-summary"],
    queryFn: getCustomersSummary,
  });

  const { data, isLoading, isFetching, error, refetch } = useQuery({
    queryKey: ["customers", segment, risk, sales, search, sort, offset],
    queryFn: () =>
      listCustomers({
        segment: segment || undefined,
        risk: risk || undefined,
        has_sales: sales === "" ? undefined : sales === "yes",
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
          <h1 className="text-xl font-semibold">Clientes</h1>
          <p className="text-sm text-muted-foreground">
            Esfuerzo frente a resultado. Los teléfonos y las direcciones se
            muestran enmascarados; revelarlos queda auditado.
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

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-8">
        <SummaryCard label="Total" value={summary.data?.total ?? 0} />
        <SummaryCard label="Nuevos (7 d)" value={summary.data?.new_7d ?? 0} />
        <SummaryCard label="Activos (7 d)" value={summary.data?.active_7d ?? 0} />
        <SummaryCard label="Compradores" value={summary.data?.buyers ?? 0} />
        <SummaryCard
          label="Conversión"
          value={`${((summary.data?.conversion ?? 0) * 100).toFixed(0)}%`}
        />
        <SummaryCard
          label="En riesgo"
          value={summary.data?.risk ?? 0}
          highlight
        />
        <SummaryCard label="Bloqueados" value={summary.data?.blocked ?? 0} />
        <SummaryCard
          label="Poco rentables"
          value={summary.data?.low_value ?? 0}
          highlight
        />
      </div>

      <div className="flex flex-wrap gap-3 text-sm">
        <span className="rounded-md border px-3 py-1.5">
          Ingresos: {money(summary.data?.revenue_usd ?? 0, summary.data?.revenue_cup ?? 0)}
        </span>
        <span className="rounded-md border px-3 py-1.5">
          Comisión cobrada:{" "}
          {money(summary.data?.commission_usd ?? 0, summary.data?.commission_cup ?? 0)}
        </span>
      </div>

      <RetentionPolicy />

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
            placeholder="Nombre, teléfono, handle o pedido"
            value={searchInput}
            onChange={(event) => setSearchInput(event.target.value)}
          />
        </div>
        <Button type="submit" variant="outline" size="sm">
          Buscar
        </Button>

        <Select
          className="w-44"
          value={segment}
          onChange={(event) => {
            setSegment(event.target.value as "" | CustomerSegment);
            setOffset(0);
          }}
        >
          <option value="">Todos los segmentos</option>
          {Object.entries(CUSTOMER_SEGMENT_LABELS).map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </Select>

        <Select
          className="w-36"
          value={risk}
          onChange={(event) => {
            setRisk(event.target.value as "" | CustomerRisk);
            setOffset(0);
          }}
        >
          <option value="">Todo riesgo</option>
          <option value="low">Riesgo bajo</option>
          <option value="medium">Riesgo medio</option>
          <option value="high">Riesgo alto</option>
        </Select>

        <Select
          className="w-40"
          value={sales}
          onChange={(event) => {
            setSales(event.target.value as "" | "yes" | "no");
            setOffset(0);
          }}
        >
          <option value="">Con o sin ventas</option>
          <option value="yes">Con ventas</option>
          <option value="no">Sin ventas</option>
        </Select>

        <Select
          className="w-44"
          value={sort}
          onChange={(event) => {
            setSort(event.target.value);
            setOffset(0);
          }}
        >
          <option value="last_seen_at">Actividad reciente</option>
          <option value="created_at">Más nuevos</option>
          <option value="sales_settled">Más compras</option>
          <option value="revenue_usd">Más ingresos (USD)</option>
          <option value="effort_score">Más esfuerzo</option>
          <option value="conversations_total">Más conversaciones</option>
          <option value="turns_total">Más turnos</option>
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
          No se pudieron cargar los clientes: {(error as Error).message}
        </p>
      )}

      <DataTable
        columns={columns}
        data={data?.items ?? []}
        onRowClick={(row) => onOpen(row.id)}
        empty={
          isLoading ? (
            "Cargando clientes…"
          ) : (
            <span className="inline-flex items-center gap-2">
              <Contact className="h-4 w-4" />
              No hay clientes con estos filtros.
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

export function CustomersPanel() {
  const { customerId } = useParams();
  const navigate = useNavigate();
  const id = customerId ? Number(customerId) : null;

  if (id !== null && Number.isFinite(id)) {
    return (
      <CustomerDetail
        customerId={id}
        onBack={() => navigate("/customers")}
        onMerged={(intoId) => navigate(`/customers/${intoId}`)}
      />
    );
  }
  return <CustomerList onOpen={(next) => navigate(`/customers/${next}`)} />;
}
