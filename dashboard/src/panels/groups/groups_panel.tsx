import { useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Plus, Pencil, Trash2 } from "lucide-react";
import {
  createBroadcastGroup,
  deleteBroadcastGroup,
  getBroadcastStrategy,
  listBroadcastGroups,
  listCategories,
  listZones,
  updateBroadcastGroup,
} from "@/lib/api";
import type {
  BroadcastGroup,
  BroadcastGroupIn,
  BroadcastStrategy,
  BroadcastChannel,
  GroupStatus,
  Sender,
} from "@/types/api";
import { GROUP_STATUS_LABELS } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { DataTable } from "@/components/data_table";
import { ConfirmDialog } from "@/components/confirm_dialog";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

const STATUS_VARIANTS: Record<GroupStatus, "success" | "secondary" | "destructive"> = {
  ACTIVE: "success",
  PAUSED: "secondary",
  BLOCKED: "destructive",
};

export function GroupsPanel() {
  const qc = useQueryClient();
  const { data = [], isLoading } = useQuery({
    queryKey: ["broadcast-groups"],
    queryFn: () => listBroadcastGroups(),
  });
  const { data: categories = [] } = useQuery({
    queryKey: ["categories"],
    queryFn: () => listCategories({ active: true }),
  });
  const { data: zones = [] } = useQuery({ queryKey: ["zones"], queryFn: listZones });
  const { data: strategy } = useQuery({
    queryKey: ["broadcast-strategy"],
    queryFn: getBroadcastStrategy,
  });

  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<BroadcastGroup | null>(null);
  const [toDelete, setToDelete] = useState<BroadcastGroup | null>(null);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["broadcast-groups"] });
    setCreating(false);
    setEditing(null);
    setToDelete(null);
  };

  const save = useMutation({
    mutationFn: (value: BroadcastGroupIn) =>
      editing ? updateBroadcastGroup(editing.id, value) : createBroadcastGroup(value),
    onSuccess: invalidate,
  });
  const remove = useMutation({
    mutationFn: (id: number) => deleteBroadcastGroup(id),
    onSuccess: invalidate,
  });

  const categoryNames = (ids: number[]) =>
    ids.length
      ? ids.map((id) => categories.find((c) => c.id === id)?.name ?? `#${id}`).join(", ")
      : "Todas";

  const zoneNames = (ids: number[]) =>
    ids.length
      ? ids.map((id) => zones.find((z) => z.id === id)?.name ?? `#${id}`).join(", ")
      : "Todas";

  const columns: ColumnDef<BroadcastGroup, unknown>[] = [
    { accessorKey: "name", header: "Grupo" },
    {
      accessorKey: "target",
      header: "Destino",
      cell: (c) => <code className="text-xs text-muted-foreground">{c.row.original.target}</code>,
    },
    { id: "zones", header: "Zonas", cell: (c) => zoneNames(c.row.original.zone_ids) },
    {
      id: "sender",
      header: "Publica",
      cell: (c) => (
        <Badge variant={c.row.original.sender === "userbot" ? "default" : "secondary"}>
          {c.row.original.sender === "userbot" ? "Userbot" : "Bot"}
        </Badge>
      ),
    },
    { id: "categories", header: "Categorías", cell: (c) => categoryNames(c.row.original.category_ids) },
    {
      accessorKey: "status",
      header: "Estado",
      cell: (c) => (
        <Badge variant={STATUS_VARIANTS[c.row.original.status]}>
          {GROUP_STATUS_LABELS[c.row.original.status]}
        </Badge>
      ),
    },
    {
      id: "limits",
      header: "Límites",
      cell: (c) => (
        <span className="text-xs text-muted-foreground">
          {c.row.original.max_posts_per_day}/día · {c.row.original.min_interval_s}-
          {c.row.original.max_interval_s} s
        </span>
      ),
    },
    {
      id: "actions",
      header: "",
      cell: (c) => (
        <div className="flex justify-end gap-1">
          <Button variant="ghost" size="icon" onClick={() => setEditing(c.row.original)}>
            <Pencil />
          </Button>
          <Button variant="ghost" size="icon" onClick={() => setToDelete(c.row.original)}>
            <Trash2 className="text-destructive" />
          </Button>
        </div>
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Grupos</h1>
          <p className="text-sm text-muted-foreground">
            Grupos de difusión, categorías permitidas y cuenta que publica en cada uno.
          </p>
        </div>
        <Button onClick={() => setCreating(true)}>
          <Plus />
          Nuevo grupo
        </Button>
      </div>

      {save.error && <p className="text-sm text-destructive">{(save.error as Error).message}</p>}
      {remove.error && <p className="text-sm text-destructive">{(remove.error as Error).message}</p>}

      {isLoading ? (
        <p className="text-muted-foreground">Cargando…</p>
      ) : (
        <DataTable
          columns={columns}
          data={data}
          empty="Sin grupos. Agrega el primero con «Nuevo grupo»."
        />
      )}

      <GroupForm
        open={creating || editing !== null}
        group={editing}
        categories={categories}
        zones={zones}
        strategy={strategy}
        loading={save.isPending}
        onClose={() => {
          setCreating(false);
          setEditing(null);
        }}
        onSave={(value) => save.mutate(value)}
      />

      <ConfirmDialog
        open={toDelete !== null}
        onOpenChange={(open) => !open && setToDelete(null)}
        title="Eliminar grupo"
        description={`¿Eliminar "${toDelete?.name}"? Si tiene envíos, el sistema pedirá bloquearlo en su lugar.`}
        confirmLabel="Eliminar"
        destructive
        loading={remove.isPending}
        onConfirm={() => toDelete && remove.mutate(toDelete.id)}
      />
    </div>
  );
}

function GroupForm({
  open,
  group,
  categories,
  zones,
  strategy,
  loading,
  onClose,
  onSave,
}: {
  open: boolean;
  group: BroadcastGroup | null;
  categories: { id: number; name: string }[];
  zones: { id: number; name: string }[];
  strategy: BroadcastStrategy | undefined;
  loading: boolean;
  onClose: () => void;
  onSave: (value: BroadcastGroupIn) => void;
}) {
  const empty: BroadcastGroupIn = {
    channel: "telegram",
    target: "",
    name: "",
    zone_ids: [],
    sender: "userbot",
    status: "ACTIVE",
    max_posts_per_day: strategy?.max_posts_per_day ?? 20,
    min_interval_s: strategy?.min_interval_s ?? 300,
    max_interval_s: strategy?.max_interval_s ?? 1200,
    category_ids: [],
    notes: "",
  };
  const [form, setForm] = useState<BroadcastGroupIn>(empty);
  const [initializedFor, setInitializedFor] = useState<number | "new" | null>(null);

  const key = group?.id ?? "new";
  if (open && initializedFor !== key) {
    setForm(
      group
        ? {
            channel: group.channel,
            target: group.target,
            name: group.name,
            zone_ids: group.zone_ids,
            sender: group.sender,
            status: group.status,
            max_posts_per_day: group.max_posts_per_day,
            min_interval_s: group.min_interval_s,
            max_interval_s: group.max_interval_s,
            category_ids: group.category_ids,
            notes: group.notes ?? "",
          }
        : empty,
    );
    setInitializedFor(key);
  }
  if (!open && initializedFor !== null) setInitializedFor(null);

  const set = <K extends keyof BroadcastGroupIn>(field: K, value: BroadcastGroupIn[K]) =>
    setForm((current) => ({ ...current, [field]: value }));

  const toggleCategory = (id: number, checked: boolean) => {
    const current = form.category_ids ?? [];
    set(
      "category_ids",
      checked ? [...current, id] : current.filter((value) => value !== id),
    );
  };

  const toggleZone = (id: number, checked: boolean) => {
    const current = form.zone_ids ?? [];
    set(
      "zone_ids",
      checked ? [...current, id] : current.filter((value) => value !== id),
    );
  };

  const invalidIntervals = (form.max_interval_s ?? 0) < (form.min_interval_s ?? 0);

  return (
    <Dialog open={open} onOpenChange={(value) => !value && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{group ? "Editar grupo" : "Nuevo grupo"}</DialogTitle>
        </DialogHeader>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Field label="Nombre" className="sm:col-span-2">
            <Input value={form.name} onChange={(e) => set("name", e.target.value)} />
          </Field>
          <Field label="Destino" className="sm:col-span-2">
            <Input
              value={form.target}
              placeholder="@grupo, -100123456789 o https://t.me/+invite"
              onChange={(e) => set("target", e.target.value)}
            />
          </Field>
          <Field label="Zonas" className="sm:col-span-2">
            <div className="max-h-40 overflow-y-auto rounded-md border p-3">
              {zones.length === 0 && (
                <p className="text-sm text-muted-foreground">
                  No hay zonas creadas (Catálogo → Zonas).
                </p>
              )}
              {zones.map((zone) => (
                <label key={zone.id} className="flex items-center gap-2 py-0.5 text-sm">
                  <input
                    type="checkbox"
                    checked={(form.zone_ids ?? []).includes(zone.id)}
                    onChange={(e) => toggleZone(zone.id, e.target.checked)}
                  />
                  {zone.name}
                </label>
              ))}
              <p className="mt-2 text-xs text-muted-foreground">
                Sin zonas = grupo sin zona asignada.
              </p>
            </div>
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
          <Field label="Publica como">
            <Select value={form.sender} onChange={(e) => set("sender", e.target.value as Sender)}>
              <option value="userbot">Userbot (Telethon)</option>
              <option value="bot">Bot (Bot API, inactivo)</option>
            </Select>
          </Field>
          <Field label="Estado">
            <Select value={form.status} onChange={(e) => set("status", e.target.value as GroupStatus)}>
              {(Object.keys(GROUP_STATUS_LABELS) as GroupStatus[]).map((value) => (
                <option key={value} value={value}>
                  {GROUP_STATUS_LABELS[value]}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Máximo por día">
            <Input
              type="number"
              min={0}
              max={100}
              value={form.max_posts_per_day}
              onChange={(e) => set("max_posts_per_day", Number(e.target.value))}
            />
          </Field>
          <Field label="Intervalo mínimo (s)">
            <Input
              type="number"
              min={0}
              value={form.min_interval_s}
              onChange={(e) => set("min_interval_s", Number(e.target.value))}
            />
          </Field>
          <Field label="Intervalo máximo (s)">
            <Input
              type="number"
              min={0}
              value={form.max_interval_s}
              onChange={(e) => set("max_interval_s", Number(e.target.value))}
            />
          </Field>
          <Field label="Categorías permitidas" className="sm:col-span-2">
            <div className="max-h-40 overflow-y-auto rounded-md border p-3">
              {categories.length === 0 && (
                <p className="text-sm text-muted-foreground">No hay categorías creadas.</p>
              )}
              {categories.map((category) => (
                <label key={category.id} className="flex items-center gap-2 py-0.5 text-sm">
                  <input
                    type="checkbox"
                    checked={(form.category_ids ?? []).includes(category.id)}
                    onChange={(e) => toggleCategory(category.id, e.target.checked)}
                  />
                  {category.name}
                </label>
              ))}
              <p className="mt-2 text-xs text-muted-foreground">
                Sin categorías = grupo libre: recibe cualquier producto.
              </p>
            </div>
          </Field>
          <Field label="Notas" className="sm:col-span-2">
            <Textarea value={form.notes ?? ""} onChange={(e) => set("notes", e.target.value)} />
          </Field>
        </div>
        {invalidIntervals && (
          <p className="mt-3 text-sm text-destructive">
            El intervalo máximo no puede ser menor que el mínimo.
          </p>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={loading}>
            Cancelar
          </Button>
          <Button
            onClick={() => onSave(form)}
            disabled={!form.name.trim() || !form.target.trim() || invalidIntervals || loading}
          >
            {loading ? "..." : "Guardar"}
          </Button>
        </DialogFooter>
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
