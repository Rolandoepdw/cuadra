import { useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Plus, Eye, Send, RefreshCw, Pencil, Trash2, Pause, Play } from "lucide-react";
import {
  createBroadcast,
  deleteBroadcast,
  getBroadcastPublishing,
  listBroadcastGroups,
  listBroadcastPosts,
  listBroadcasts,
  listProducts,
  reevaluateBroadcasts,
  sendBroadcastNow,
  setBroadcastEnabled,
  setBroadcastPublishing,
  updateBroadcast,
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
};

export function BroadcastsPanel() {
  const qc = useQueryClient();
  const { data = [], isLoading } = useQuery({ queryKey: ["broadcasts"], queryFn: listBroadcasts });
  const { data: products = [] } = useQuery({ queryKey: ["products"], queryFn: () => listProducts() });
  const { data: groups = [] } = useQuery({
    queryKey: ["broadcast-groups"],
    queryFn: () => listBroadcastGroups(),
  });
  const { data: publishing } = useQuery({
    queryKey: ["broadcast-publishing"],
    queryFn: getBroadcastPublishing,
  });
  const paused = publishing?.publishing === false;

  const [creating, setCreating] = useState(false);
  const [logFor, setLogFor] = useState<Broadcast | null>(null);
  const [toDelete, setToDelete] = useState<Broadcast | null>(null);
  const [editingBroadcast, setEditingBroadcast] = useState<Broadcast | null>(null);
  const [reevalMsg, setReevalMsg] = useState<string | null>(null);
  const [confirmReevaluate, setConfirmReevaluate] = useState(false);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["broadcasts"] });
    setCreating(false);
    setEditingBroadcast(null);
  };

  const save = useMutation({
    mutationFn: (value: BroadcastIn) => createBroadcast(value),
    onSuccess: invalidate,
  });
  const togglePublishing = useMutation({
    mutationFn: (value: boolean) => setBroadcastPublishing(value),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["broadcast-publishing"] });
      qc.invalidateQueries({ queryKey: ["broadcasts"] });
      setReevalMsg(null);
    },
  });
  const toggleEnabled = useMutation({
    mutationFn: ({ id, enabled }: { id: number; enabled: boolean }) =>
      setBroadcastEnabled(id, enabled),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["broadcasts"] }),
  });
  const sendNow = useMutation({
    mutationFn: (id: number) => sendBroadcastNow(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["broadcasts"] }),
  });
  const reevaluate = useMutation({
    mutationFn: () => reevaluateBroadcasts(),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["broadcasts"] });
      qc.invalidateQueries({ queryKey: ["broadcast-publishing"] });
      const resumed = res.resumed ? "Publicaciones reanudadas. " : "";
      setReevalMsg(
        `${resumed}Reevaluadas ${res.checked}: ${res.queued} encoladas, ${res.waiting} siguen en espera, ${res.failed} sin destino.`,
      );
    },
  });
  const runReevaluate = () => {
    setReevalMsg(null);
    reevaluate.mutate();
  };
  const update = useMutation({
    mutationFn: ({ id, value }: { id: number; value: BroadcastIn }) => updateBroadcast(id, value),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["broadcasts"] });
      setEditingBroadcast(null);
    },
  });
  const remove = useMutation({
    mutationFn: (id: number) => deleteBroadcast(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["broadcasts"] });
      setToDelete(null);
    },
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
      id: "enabled",
      header: "Publicar",
      cell: (c) => {
        const broadcast = c.row.original;
        return (
          <label className="flex items-center gap-2 text-xs">
            <input
              type="checkbox"
              checked={broadcast.enabled}
              disabled={toggleEnabled.isPending}
              title="Habilitar o deshabilitar las publicaciones de esta campaña"
              onChange={(e) => toggleEnabled.mutate({ id: broadcast.id, enabled: e.target.checked })}
            />
            <span className={broadcast.enabled ? "text-foreground" : "text-muted-foreground"}>
              {broadcast.enabled ? "Habilitada" : "Deshabilitada"}
            </span>
          </label>
        );
      },
    },
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
              title="Enviar ahora (no respeta la pausa global)"
              disabled={!broadcast.enabled || (!active && broadcast.status !== "FAILED")}
              onClick={() => sendNow.mutate(broadcast.id)}
            >
              <Send />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              title="Editar"
              onClick={() => setEditingBroadcast(broadcast)}
            >
              <Pencil />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              title="Eliminar"
              onClick={() => setToDelete(broadcast)}
            >
              <Trash2 className="text-destructive" />
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
          <div className="flex items-center gap-2">
            <h1 className="text-2xl font-semibold tracking-tight">Difusiones</h1>
            <Badge variant={paused ? "warning" : "success"}>
              {paused ? "Pausadas" : "Publicando"}
            </Badge>
          </div>
          <p className="text-sm text-muted-foreground">
            Campañas con spintax, filtro por categoría y log por grupo.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant={paused ? "default" : "outline"}
            disabled={togglePublishing.isPending}
            title={
              paused
                ? "Reanuda las publicaciones; cada campaña continúa donde se quedó."
                : "Detiene la programación de todas las campañas. No afecta a los chats 1:1."
            }
            onClick={() => togglePublishing.mutate(paused)}
          >
            {paused ? <Play /> : <Pause />}
            {togglePublishing.isPending
              ? "..."
              : paused
                ? "Reanudar publicaciones"
                : "Pausar publicaciones"}
          </Button>
          <Button
            variant="outline"
            disabled={reevaluate.isPending}
            title={
              paused
                ? "Reanuda las publicaciones y vuelve a evaluar las campañas habilitadas."
                : "Vuelve a evaluar las campañas habilitadas ahora, sin esperar la fecha programada."
            }
            onClick={() => {
              setReevalMsg(null);
              if (paused) setConfirmReevaluate(true);
              else runReevaluate();
            }}
          >
            <RefreshCw />
            {reevaluate.isPending ? "Reevaluando…" : "Reevaluar"}
          </Button>
          <Button onClick={() => setCreating(true)}>
            <Plus />
            Nueva difusión
          </Button>
        </div>
      </div>

      {reevalMsg && <p className="text-sm text-muted-foreground">{reevalMsg}</p>}
      {reevaluate.error && (
        <p className="text-sm text-destructive">{(reevaluate.error as Error).message}</p>
      )}
      {togglePublishing.error && (
        <p className="text-sm text-destructive">{(togglePublishing.error as Error).message}</p>
      )}
      {save.error && <p className="text-sm text-destructive">{(save.error as Error).message}</p>}
      {sendNow.error && <p className="text-sm text-destructive">{(sendNow.error as Error).message}</p>}
      {toggleEnabled.error && (
        <p className="text-sm text-destructive">{(toggleEnabled.error as Error).message}</p>
      )}
      {update.error && <p className="text-sm text-destructive">{(update.error as Error).message}</p>}
      {remove.error && <p className="text-sm text-destructive">{(remove.error as Error).message}</p>}

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

      <BroadcastForm
        key={editingBroadcast?.id ?? "new"}
        open={editingBroadcast !== null}
        products={products}
        groups={groups}
        loading={update.isPending}
        initial={editingBroadcast ?? undefined}
        onClose={() => setEditingBroadcast(null)}
        onSave={(value) => editingBroadcast && update.mutate({ id: editingBroadcast.id, value })}
      />

      {logFor && (
        <BroadcastLog broadcast={logFor} open={logFor !== null} onClose={() => setLogFor(null)} />
      )}

      <ConfirmDialog
        open={confirmReevaluate}
        onOpenChange={(open) => !open && setConfirmReevaluate(false)}
        title="Reanudar y reevaluar"
        description="Las publicaciones están pausadas. Reevaluar reanuda la publicación global y vuelve a evaluar de inmediato las campañas habilitadas."
        confirmLabel="Reanudar y reevaluar"
        loading={reevaluate.isPending}
        onConfirm={() => {
          setConfirmReevaluate(false);
          runReevaluate();
        }}
      />

      <ConfirmDialog
        open={toDelete !== null}
        onOpenChange={(open) => !open && setToDelete(null)}
        title="Eliminar difusión"
        description="Se eliminará la campaña junto con todo su historial de envíos. Esta acción no se puede deshacer; para dejar de publicar sin perder el historial, deshabilita la campaña."
        confirmLabel="Eliminar definitivamente"
        destructive
        loading={remove.isPending}
        onConfirm={() => toDelete && remove.mutate(toDelete.id)}
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
  initial,
}: {
  open: boolean;
  products: { id: number; name: string }[];
  groups: { id: number; name: string; sender: string; status: string; category_ids: number[] }[];
  loading: boolean;
  onClose: () => void;
  onSave: (value: BroadcastIn) => void;
  initial?: BroadcastIn | null;
}) {
  const empty: BroadcastIn = { channel: "telegram", template: "", product_id: null, group_ids: [] };
  const [form, setForm] = useState<BroadcastIn>(
    initial ? { ...initial, group_ids: initial.group_ids ?? [] } : empty,
  );
  const [initialized, setInitialized] = useState(false);

  if (open && !initialized) {
    setForm(initial ? { ...initial, group_ids: initial.group_ids ?? [] } : empty);
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
          <DialogTitle>{initial ? "Editar difusión" : "Nueva difusión"}</DialogTitle>
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
            {loading ? "..." : initial ? "Guardar cambios" : "Crear difusión"}
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
