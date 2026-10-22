import { useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Plus, Eye, Send, Ban } from "lucide-react";
import {
  cancelBroadcast,
  createBroadcast,
  listBroadcastGroups,
  listBroadcastPosts,
  listBroadcasts,
  listProducts,
  sendBroadcastNow,
} from "@/lib/api";
import type {
  Broadcast,
  BroadcastChannel,
  BroadcastIn,
  BroadcastPost,
  BroadcastStatus,
} from "@/types/api";
import { BROADCAST_STATUS_LABELS, formatDateTime } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { DataTable } from "@/components/data_table";
import { ConfirmDialog } from "@/components/confirm_dialog";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

const STATUS_VARIANTS: Record<
  BroadcastStatus,
  "success" | "secondary" | "destructive" | "warning" | "default"
> = {
  SCHEDULED: "secondary",
  SENDING: "default",
  COMPLETED: "success",
  FAILED: "destructive",
  CANCELLED: "secondary",
};

export function BroadcastsPanel() {
  const qc = useQueryClient();
  const { data = [], isLoading } = useQuery({ queryKey: ["broadcasts"], queryFn: listBroadcasts });
  const { data: products = [] } = useQuery({ queryKey: ["products"], queryFn: () => listProducts() });
  const { data: groups = [] } = useQuery({
    queryKey: ["broadcast-groups"],
    queryFn: () => listBroadcastGroups(),
  });

  const [creating, setCreating] = useState(false);
  const [logFor, setLogFor] = useState<Broadcast | null>(null);
  const [toCancel, setToCancel] = useState<Broadcast | null>(null);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["broadcasts"] });
    setCreating(false);
    setToCancel(null);
  };

  const save = useMutation({
    mutationFn: (value: BroadcastIn) => createBroadcast(value),
    onSuccess: invalidate,
  });
  const cancel = useMutation({
    mutationFn: (id: number) => cancelBroadcast(id),
    onSuccess: invalidate,
  });
  const sendNow = useMutation({
    mutationFn: (id: number) => sendBroadcastNow(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["broadcasts"] }),
  });

  const productName = (id: number | null) =>
    id ? products.find((product) => product.id === id)?.name ?? `#${id}` : "Solo plantilla";

  const columns: ColumnDef<Broadcast, unknown>[] = [
    {
      accessorKey: "id",
      header: "ID",
      cell: (c) => <span className="tabular-nums text-muted-foreground">{c.row.original.id}</span>,
    },
    {
      id: "template",
      header: "Mensaje",
      cell: (c) => (
        <span className="line-clamp-2 max-w-md text-sm" title={c.row.original.template}>
          {c.row.original.template}
        </span>
      ),
    },
    { id: "product", header: "Producto", cell: (c) => productName(c.row.original.product_id) },
    {
      accessorKey: "status",
      header: "Estado",
      cell: (c) => (
        <Badge variant={STATUS_VARIANTS[c.row.original.status]}>
          {BROADCAST_STATUS_LABELS[c.row.original.status]}
        </Badge>
      ),
    },
    {
      id: "progress",
      header: "Envíos",
      cell: (c) => (
        <span className="text-xs text-muted-foreground">
          {c.row.original.posts_sent} ok · {c.row.original.posts_failed} fallidos
          {c.row.original.group_count > 0 ? ` · ${c.row.original.group_count} manuales` : ""}
        </span>
      ),
    },
    {
      id: "next",
      header: "Próximo",
      cell: (c) => (
        <span className="text-xs text-muted-foreground">{formatDateTime(c.row.original.next_send_at)}</span>
      ),
    },
    {
      id: "actions",
      header: "",
      cell: (c) => {
        const broadcast = c.row.original;
        const active = broadcast.status === "SCHEDULED" || broadcast.status === "SENDING";
        return (
          <div className="flex justify-end gap-1">
            <Button variant="ghost" size="icon" title="Ver envíos" onClick={() => setLogFor(broadcast)}>
              <Eye />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              title="Programar ahora"
              disabled={!active && broadcast.status !== "FAILED"}
              onClick={() => sendNow.mutate(broadcast.id)}
            >
              <Send />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              title="Cancelar"
              disabled={!active}
              onClick={() => setToCancel(broadcast)}
            >
              <Ban className="text-destructive" />
            </Button>
          </div>
        );
      },
    },
  ];

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Difusiones</h1>
          <p className="text-sm text-muted-foreground">
            Campañas con spintax, filtro por categoría y log por grupo.
          </p>
        </div>
        <Button onClick={() => setCreating(true)}>
          <Plus />
          Nueva difusión
        </Button>
      </div>

      {save.error && <p className="text-sm text-destructive">{(save.error as Error).message}</p>}
      {sendNow.error && <p className="text-sm text-destructive">{(sendNow.error as Error).message}</p>}
      {cancel.error && <p className="text-sm text-destructive">{(cancel.error as Error).message}</p>}

      {isLoading ? (
        <p className="text-muted-foreground">Cargando…</p>
      ) : (
        <DataTable
          columns={columns}
          data={data}
          empty="Sin difusiones. Crea una con «Nueva difusión»."
        />
      )}

      <BroadcastForm
        open={creating}
        products={products}
        groups={groups}
        loading={save.isPending}
        onClose={() => setCreating(false)}
        onSave={(value) => save.mutate(value)}
      />

      {logFor && (
        <BroadcastLog broadcast={logFor} open={logFor !== null} onClose={() => setLogFor(null)} />
      )}

      <ConfirmDialog
        open={toCancel !== null}
        onOpenChange={(open) => !open && setToCancel(null)}
        title="Cancelar difusión"
        description="Se dejan de programar envíos; los posts ya publicados se conservan en el log."
        confirmLabel="Cancelar difusión"
        destructive
        loading={cancel.isPending}
        onConfirm={() => toCancel && cancel.mutate(toCancel.id)}
      />
    </div>
  );
}

function BroadcastForm({
  open,
  products,
  groups,
  loading,
  onClose,
  onSave,
}: {
  open: boolean;
  products: { id: number; name: string }[];
  groups: { id: number; name: string; sender: string; status: string; category_ids: number[] }[];
  loading: boolean;
  onClose: () => void;
  onSave: (value: BroadcastIn) => void;
}) {
  const empty: BroadcastIn = { channel: "telegram", template: "", product_id: null, group_ids: [] };
  const [form, setForm] = useState<BroadcastIn>(empty);
  const [initialized, setInitialized] = useState(false);

  if (open && !initialized) {
    setForm(empty);
    setInitialized(true);
  }
  if (!open && initialized) setInitialized(false);

  const set = <K extends keyof BroadcastIn>(field: K, value: BroadcastIn[K]) =>
    setForm((current) => ({ ...current, [field]: value }));

  const toggleGroup = (id: number, checked: boolean) => {
    const current = form.group_ids ?? [];
    set("group_ids", checked ? [...current, id] : current.filter((value) => value !== id));
  };

  return (
    <Dialog open={open} onOpenChange={(value) => !value && onClose()}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Nueva difusión</DialogTitle>
          <DialogDescription>
            El primer envío se programa de inmediato. Luego avanza un grupo por tick según los
            intervalos del grupo.
          </DialogDescription>
        </DialogHeader>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Field label="Plantilla" className="sm:col-span-2">
            <Textarea
              className="min-h-[140px] font-mono text-xs"
              value={form.template}
              onChange={(e) => set("template", e.target.value)}
              placeholder={"¡{Hola|Buenas}! Tengo {producto} a {precio} {moneda}.\nEntrega en {zona}.\nID: {id}"}
            />
            <p className="text-xs text-muted-foreground">
              Spintax: <code>{"{Hola|Buenas}"}</code>. Variables:{" "}
              <code>{"{id} {producto} {precio} {moneda} {zona} {proveedor} {categoria}"}</code>.
              Si no usas <code>{"{id}"}</code>, se agrega al final como <code>ID: 42</code>.
            </p>
          </Field>
          <Field label="Producto">
            <Select
              value={form.product_id ?? ""}
              onChange={(e) => set("product_id", e.target.value ? Number(e.target.value) : null)}
            >
              <option value="">Sin producto (solo plantilla)</option>
              {products.map((product) => (
                <option key={product.id} value={product.id}>
                  {product.name}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Canal">
            <Select
              value={form.channel}
              onChange={(e) => set("channel", e.target.value as BroadcastChannel)}
            >
              <option value="telegram">Telegram</option>
              <option value="whatsapp" disabled>
                WhatsApp (Fase 2)
              </option>
            </Select>
          </Field>
          <Field label="Grupos (opcional)" className="sm:col-span-2">
            <div className="max-h-48 overflow-y-auto rounded-md border p-3">
              {groups.length === 0 && (
                <p className="text-sm text-muted-foreground">No hay grupos registrados.</p>
              )}
              {groups.map((group) => (
                <label key={group.id} className="flex items-center gap-2 py-0.5 text-sm">
                  <input
                    type="checkbox"
                    checked={(form.group_ids ?? []).includes(group.id)}
                    onChange={(e) => toggleGroup(group.id, e.target.checked)}
                  />
                  <span>{group.name}</span>
                  <Badge variant="secondary">{group.sender === "userbot" ? "Userbot" : "Bot"}</Badge>
                  {group.status !== "ACTIVE" && <Badge variant="warning">{group.status}</Badge>}
                </label>
              ))}
              <p className="mt-2 text-xs text-muted-foreground">
                Sin selección = automático: producto sin categoría solo va a grupos libres; un
                grupo con categorías solo recibe las suyas.
              </p>
            </div>
          </Field>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={loading}>
            Cancelar
          </Button>
          <Button onClick={() => onSave(form)} disabled={!form.template.trim() || loading}>
            {loading ? "..." : "Crear difusión"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function BroadcastLog({
  broadcast,
  open,
  onClose,
}: {
  broadcast: Broadcast;
  open: boolean;
  onClose: () => void;
}) {
  const { data = [], isLoading } = useQuery({
    queryKey: ["broadcast-posts", broadcast.id],
    queryFn: () => listBroadcastPosts(broadcast.id),
    enabled: open,
    refetchInterval: open ? 10_000 : false,
  });

  const columns: ColumnDef<BroadcastPost, unknown>[] = [
    { accessorKey: "group_name", header: "Grupo" },
    {
      id: "sender",
      header: "Cuenta",
      cell: (c) => (c.row.original.group_sender === "userbot" ? "Userbot" : "Bot"),
    },
    {
      accessorKey: "status",
      header: "Estado",
      cell: (c) => (
        <Badge variant={c.row.original.status === "SENT" ? "success" : "destructive"}>
          {c.row.original.status === "SENT" ? "Enviado" : "Falló"}
        </Badge>
      ),
    },
    {
      id: "text",
      header: "Texto",
      cell: (c) => (
        <span className="line-clamp-2 max-w-xs text-xs" title={c.row.original.sent_text}>
          {c.row.original.sent_text}
        </span>
      ),
    },
    {
      accessorKey: "sent_at",
      header: "Fecha",
      cell: (c) => (
        <span className="text-xs text-muted-foreground">{formatDateTime(c.row.original.sent_at)}</span>
      ),
    },
    {
      id: "error",
      header: "Error",
      cell: (c) => (
        <span className="text-xs text-destructive" title={c.row.original.error ?? ""}>
          {c.row.original.error ? c.row.original.error.slice(0, 60) : "—"}
        </span>
      ),
    },
  ];

  return (
    <Dialog open={open} onOpenChange={(value) => !value && onClose()}>
      <DialogContent className="max-w-4xl">
        <DialogHeader>
          <DialogTitle>Envíos de la difusión #{broadcast.id}</DialogTitle>
          <DialogDescription>
            {broadcast.posts_sent} enviados · {broadcast.posts_failed} fallidos. Se actualiza solo.
          </DialogDescription>
        </DialogHeader>
        {isLoading ? (
          <p className="text-muted-foreground">Cargando…</p>
        ) : (
          <DataTable columns={columns} data={data} empty="Aún no hay envíos para esta difusión." />
        )}
      </DialogContent>
    </Dialog>
  );
}

function Field({
  label,
  children,
  className,
}: {
  label: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={`flex flex-col gap-2 ${className ?? ""}`}>
      <Label>{label}</Label>
      {children}
    </div>
  );
}

