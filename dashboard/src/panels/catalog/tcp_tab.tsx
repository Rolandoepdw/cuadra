import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Plus, Pencil, Trash2 } from "lucide-react";
import { createSupplier, deleteSupplier, listSuppliers, listZones, updateSupplier } from "@/lib/api";
import type { Supplier, SupplierIn } from "@/types/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
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

const EMPTY: SupplierIn = {
  name: "",
  business_alias: "",
  whatsapp_phone: "",
  telegram_id: "",
  delivery_zone: "",
  delivery_zone_ids: [],
  payment_terms: "",
  priority: 100,
  notes: "",
  active: true,
};

export function TcpTab() {
  const qc = useQueryClient();
  const { data = [], isLoading } = useQuery({ queryKey: ["suppliers"], queryFn: listSuppliers });
  const { data: zones = [] } = useQuery({ queryKey: ["zones"], queryFn: listZones });
  const [editing, setEditing] = useState<Supplier | null>(null);
  const [creating, setCreating] = useState(false);
  const [toDelete, setToDelete] = useState<Supplier | null>(null);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["suppliers"] });
    setEditing(null);
    setCreating(false);
    setToDelete(null);
  };

  const save = useMutation({
    mutationFn: (v: SupplierIn) => (editing ? updateSupplier(editing.id, v) : createSupplier(v)),
    onSuccess: invalidate,
  });
  const remove = useMutation({ mutationFn: (id: number) => deleteSupplier(id), onSuccess: invalidate });

  const columns: ColumnDef<Supplier, unknown>[] = [
    { accessorKey: "name", header: "Nombre" },
    { accessorKey: "business_alias", header: "Alias", cell: (c) => c.row.original.business_alias ?? "—" },
    { accessorKey: "telegram_id", header: "Telegram", cell: (c) => c.row.original.telegram_id ?? "—" },
    {
      accessorKey: "delivery_zone_ids",
      header: "Zonas",
      cell: (c) =>
        (c.row.original.delivery_zone_ids ?? [])
          .map((id) => zones.find((z) => z.id === id)?.name ?? `#${id}`)
          .filter(Boolean)
          .join(", ") || c.row.original.delivery_zone || "—",
    },
    {
      accessorKey: "priority",
      header: "Prioridad",
      cell: (c) => <span className="tabular-nums">{c.row.original.priority}</span>,
    },
    {
      accessorKey: "active",
      header: "Estado",
      cell: (c) => (
        <Badge variant={c.row.original.active ? "success" : "secondary"}>
          {c.row.original.active ? "activo" : "inactivo"}
        </Badge>
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
    <div className="flex flex-col gap-4">
      <div className="flex justify-end">
        <Button onClick={() => setCreating(true)}>
          <Plus />
          Nuevo TCP
        </Button>
      </div>
      {isLoading ? <p className="text-muted-foreground">Cargando…</p> : <DataTable columns={columns} data={data} />}

      <SupplierForm
        open={creating || editing !== null}
        supplier={editing}
        loading={save.isPending}
        onClose={() => {
          setCreating(false);
          setEditing(null);
        }}
        onSave={(v) => save.mutate(v)}
      />

      <ConfirmDialog
        open={toDelete !== null}
        onOpenChange={(o) => !o && setToDelete(null)}
        title="Desactivar TCP"
        description={`¿Desactivar "${toDelete?.name}"? Sus productos quedan fuera de circulación.`}
        confirmLabel="Desactivar"
        destructive
        loading={remove.isPending}
        onConfirm={() => toDelete && remove.mutate(toDelete.id)}
      />
    </div>
  );
}

function SupplierForm({
  open,
  supplier,
  loading,
  onClose,
  onSave,
}: {
  open: boolean;
  supplier: Supplier | null;
  loading: boolean;
  onClose: () => void;
  onSave: (v: SupplierIn) => void;
}) {
  const [form, setForm] = useState<SupplierIn>(EMPTY);
  const [initializedFor, setInitializedFor] = useState<number | "new" | null>(null);
  const { data: zones = [] } = useQuery({ queryKey: ["zones"], queryFn: listZones });

  const key = supplier?.id ?? "new";
  if (open && initializedFor !== key) {
    setForm(
      supplier
        ? {
            name: supplier.name,
            business_alias: supplier.business_alias ?? "",
            whatsapp_phone: supplier.whatsapp_phone ?? "",
            telegram_id: supplier.telegram_id ?? "",
            delivery_zone: supplier.delivery_zone ?? "",
            delivery_zone_ids: supplier.delivery_zone_ids ?? [],
            payment_terms: supplier.payment_terms ?? "",
            priority: supplier.priority,
            notes: supplier.notes ?? "",
            active: supplier.active,
          }
        : EMPTY,
    );
    setInitializedFor(key);
  }
  if (!open && initializedFor !== null) setInitializedFor(null);

  const set = <K extends keyof SupplierIn>(k: K, v: SupplierIn[K]) => setForm((f) => ({ ...f, [k]: v }));

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{supplier ? "Editar TCP" : "Nuevo TCP"}</DialogTitle>
        </DialogHeader>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Field label="Nombre">
            <Input value={form.name} onChange={(e) => set("name", e.target.value)} />
          </Field>
          <Field label="Alias">
            <Input value={form.business_alias ?? ""} onChange={(e) => set("business_alias", e.target.value)} />
          </Field>
          <Field label="Telegram ID">
            <Input value={form.telegram_id ?? ""} onChange={(e) => set("telegram_id", e.target.value)} />
          </Field>
          <Field label="WhatsApp">
            <Input value={form.whatsapp_phone ?? ""} onChange={(e) => set("whatsapp_phone", e.target.value)} />
          </Field>
          <Field label="Zonas de entrega" className="sm:col-span-2">
            <div className="max-h-48 overflow-y-auto rounded-md border p-3">
              {zones.length === 0 && <p className="text-sm text-muted-foreground">Sin zonas registradas.</p>}
              {zones.map((zone) => (
                <label key={zone.id} className="flex items-center gap-2 py-0.5 text-sm">
                  <input
                    type="checkbox"
                    checked={(form.delivery_zone_ids ?? []).includes(zone.id)}
                    onChange={(e) =>
                      set(
                        "delivery_zone_ids",
                        e.target.checked
                          ? [...(form.delivery_zone_ids ?? []), zone.id]
                          : (form.delivery_zone_ids ?? []).filter((v) => v !== zone.id),
                      )
                    }
                  />
                  <span>{zone.name}</span>
                </label>
              ))}
              <p className="mt-2 text-xs text-muted-foreground">Sin selección = repartir en todas las zonas.</p>
            </div>
          </Field>
          <Field label="Prioridad">
            <Input
              type="number"
              value={form.priority ?? 100}
              onChange={(e) => set("priority", Number(e.target.value))}
            />
          </Field>
          <Field label="Condiciones de pago" className="sm:col-span-2">
            <Input value={form.payment_terms ?? ""} onChange={(e) => set("payment_terms", e.target.value)} />
          </Field>
          <Field label="Notas" className="sm:col-span-2">
            <Textarea value={form.notes ?? ""} onChange={(e) => set("notes", e.target.value)} />
          </Field>
          <label className="flex items-center gap-2 text-sm sm:col-span-2">
            <input type="checkbox" checked={form.active ?? true} onChange={(e) => set("active", e.target.checked)} />
            Activo
          </label>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={loading}>
            Cancelar
          </Button>
          <Button onClick={() => onSave(form)} disabled={!form.name.trim() || loading}>
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
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={`flex flex-col gap-2 ${className ?? ""}`}>
      <Label>{label}</Label>
      {children}
    </div>
  );
}
