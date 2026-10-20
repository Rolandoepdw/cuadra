import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Plus, Pencil, Trash2 } from "lucide-react";
import { createCategory, deleteCategory, listCategories, updateCategory } from "@/lib/api";
import type { Category } from "@/tipos/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { DataTable } from "@/components/data-table";
import { ConfirmDialog } from "@/components/confirm-dialog";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

export function CategoriasTab() {
  const qc = useQueryClient();
  const { data = [], isLoading } = useQuery({ queryKey: ["categories"], queryFn: listCategories });
  const [editing, setEditing] = useState<Category | null>(null);
  const [creating, setCreating] = useState(false);
  const [toDelete, setToDelete] = useState<Category | null>(null);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["categories"] });
    setEditing(null);
    setCreating(false);
    setToDelete(null);
  };

  const save = useMutation({
    mutationFn: (v: { name: string; active: boolean }) =>
      editing ? updateCategory(editing.id, v) : createCategory(v),
    onSuccess: invalidate,
  });
  const remove = useMutation({ mutationFn: (id: number) => deleteCategory(id), onSuccess: invalidate });

  const columns: ColumnDef<Category, unknown>[] = [
    { accessorKey: "id", header: "ID", cell: (c) => <span className="tabular-nums text-muted-foreground">{c.row.original.id}</span> },
    { accessorKey: "name", header: "Nombre" },
    {
      accessorKey: "active",
      header: "Estado",
      cell: (c) => (
        <Badge variant={c.row.original.active ? "success" : "secondary"}>
          {c.row.original.active ? "activa" : "inactiva"}
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
          Nueva categoría
        </Button>
      </div>
      {isLoading ? <p className="text-muted-foreground">Cargando…</p> : <DataTable columns={columns} data={data} />}

      <CategoryForm
        open={creating || editing !== null}
        category={editing}
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
        title="Eliminar categoría"
        description={`¿Eliminar "${toDelete?.name}"? Los productos asociados quedarán sin categoría.`}
        confirmLabel="Eliminar"
        destructive
        loading={remove.isPending}
        onConfirm={() => toDelete && remove.mutate(toDelete.id)}
      />
    </div>
  );
}

function CategoryForm({
  open,
  category,
  loading,
  onClose,
  onSave,
}: {
  open: boolean;
  category: Category | null;
  loading: boolean;
  onClose: () => void;
  onSave: (v: { name: string; active: boolean }) => void;
}) {
  const [name, setName] = useState("");
  const [active, setActive] = useState(true);
  const [initializedFor, setInitializedFor] = useState<number | "new" | null>(null);

  const key = category?.id ?? "new";
  if (open && initializedFor !== key) {
    setName(category?.name ?? "");
    setActive(category?.active ?? true);
    setInitializedFor(key);
  }
  if (!open && initializedFor !== null) setInitializedFor(null);

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{category ? "Editar categoría" : "Nueva categoría"}</DialogTitle>
        </DialogHeader>
        <div className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <Label htmlFor="cat-name">Nombre</Label>
            <Input id="cat-name" value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={active} onChange={(e) => setActive(e.target.checked)} />
            Activa
          </label>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={loading}>
            Cancelar
          </Button>
          <Button onClick={() => onSave({ name: name.trim(), active })} disabled={!name.trim() || loading}>
            {loading ? "..." : "Guardar"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
