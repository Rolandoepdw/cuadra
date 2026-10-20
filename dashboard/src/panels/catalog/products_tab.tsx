import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Plus, Pencil, Trash2, ImageIcon } from "lucide-react";
import {
  createProduct,
  deleteProduct,
  listCategories,
  listProducts,
  listSuppliers,
  updateProduct,
} from "@/lib/api";
import type { Currency, Product, ProductIn, ProductStatus } from "@/types/api";
import { formatMoney, STATUS_LABELS, STATUS_VARIANTS } from "@/lib/format";
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
import { ProductImages } from "./product_images";

const STATUSES: ProductStatus[] = ["ACTIVE", "OUT_OF_STOCK", "PAUSED", "REMOVED"];

export function ProductsTab() {
  const qc = useQueryClient();
  const { data = [], isLoading } = useQuery({ queryKey: ["products"], queryFn: () => listProducts() });
  const { data: suppliers = [] } = useQuery({ queryKey: ["suppliers"], queryFn: listSuppliers });
  const { data: categories = [] } = useQuery({ queryKey: ["categories"], queryFn: listCategories });

  const [editing, setEditing] = useState<Product | null>(null);
  const [creating, setCreating] = useState(false);
  const [imagesFor, setImagesFor] = useState<Product | null>(null);
  const [toDelete, setToDelete] = useState<Product | null>(null);

  const supplierName = (id: number) => suppliers.find((s) => s.id === id)?.name ?? `#${id}`;
  const categoryName = (id: number | null) => (id ? categories.find((c) => c.id === id)?.name ?? `#${id}` : "—");

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["products"] });
    setEditing(null);
    setCreating(false);
    setToDelete(null);
  };

  const save = useMutation({
    mutationFn: (v: ProductIn) => (editing ? updateProduct(editing.id, v) : createProduct(v)),
    onSuccess: invalidate,
  });
  const remove = useMutation({ mutationFn: (id: number) => deleteProduct(id), onSuccess: invalidate });

  const columns: ColumnDef<Product, unknown>[] = [
    { accessorKey: "name", header: "Producto" },
    { id: "supplier", header: "TCP", cell: (c) => supplierName(c.row.original.supplier_id) },
    { id: "category", header: "Categoría", cell: (c) => categoryName(c.row.original.category_id) },
    {
      accessorKey: "price",
      header: "Precio",
      cell: (c) => <span className="tabular-nums">{formatMoney(c.row.original.price, c.row.original.currency)}</span>,
    },
    {
      id: "stock",
      header: "Stock",
      cell: (c) => <span className="tabular-nums">{c.row.original.stock ?? "—"}</span>,
    },
    {
      accessorKey: "status",
      header: "Estado",
      cell: (c) => {
        const s = c.row.original.status;
        return <Badge variant={STATUS_VARIANTS[s]}>{STATUS_LABELS[s]}</Badge>;
      },
    },
    {
      id: "actions",
      header: "",
      cell: (c) => (
        <div className="flex justify-end gap-1">
          <Button variant="ghost" size="icon" title="Imágenes" onClick={() => setImagesFor(c.row.original)}>
            <ImageIcon />
          </Button>
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
          Nuevo producto
        </Button>
      </div>
      {isLoading ? <p className="text-muted-foreground">Cargando…</p> : <DataTable columns={columns} data={data} />}

      <ProductForm
        open={creating || editing !== null}
        product={editing}
        suppliers={suppliers}
        categories={categories}
        loading={save.isPending}
        onClose={() => {
          setCreating(false);
          setEditing(null);
        }}
        onSave={(v) => save.mutate(v)}
      />

      {imagesFor && (
        <ProductImages
          product={imagesFor}
          open={imagesFor !== null}
          onClose={() => setImagesFor(null)}
        />
      )}

      <ConfirmDialog
        open={toDelete !== null}
        onOpenChange={(o) => !o && setToDelete(null)}
        title="Eliminar producto"
        description={`¿Eliminar "${toDelete?.name}"? Se marca como REMOVED (las ventas lo referencian).`}
        confirmLabel="Eliminar"
        destructive
        loading={remove.isPending}
        onConfirm={() => toDelete && remove.mutate(toDelete.id)}
      />
    </div>
  );
}

function ProductForm({
  open,
  product,
  suppliers,
  categories,
  loading,
  onClose,
  onSave,
}: {
  open: boolean;
  product: Product | null;
  suppliers: { id: number; name: string }[];
  categories: { id: number; name: string }[];
  loading: boolean;
  onClose: () => void;
  onSave: (v: ProductIn) => void;
}) {
  const empty: ProductIn = {
    supplier_id: suppliers[0]?.id ?? 0,
    category_id: null,
    name: "",
    description: "",
    price: 0,
    currency: "USD",
    commission: 0,
    commission_currency: "USD",
    stock: 1,
    status: "ACTIVE",
  };
  const [form, setForm] = useState<ProductIn>(empty);
  const [initializedFor, setInitializedFor] = useState<number | "new" | null>(null);

  const key = product?.id ?? "new";
  if (open && initializedFor !== key) {
    setForm(
      product
        ? {
            supplier_id: product.supplier_id,
            category_id: product.category_id,
            name: product.name,
            description: product.description ?? "",
            price: product.price,
            currency: product.currency,
            commission: product.commission,
            commission_currency: product.commission_currency,
            stock: product.stock,
            status: product.status,
          }
        : empty,
    );
    setInitializedFor(key);
  }
  if (!open && initializedFor !== null) setInitializedFor(null);

  const set = <K extends keyof ProductIn>(k: K, v: ProductIn[K]) => setForm((f) => ({ ...f, [k]: v }));

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{product ? "Editar producto" : "Nuevo producto"}</DialogTitle>
        </DialogHeader>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Field label="Nombre" className="sm:col-span-2">
            <Input value={form.name} onChange={(e) => set("name", e.target.value)} />
          </Field>
          <Field label="Descripción" className="sm:col-span-2">
            <Textarea value={form.description ?? ""} onChange={(e) => set("description", e.target.value)} />
          </Field>
          <Field label="TCP">
            <Select value={form.supplier_id} onChange={(e) => set("supplier_id", Number(e.target.value))}>
              {suppliers.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Categoría">
            <Select
              value={form.category_id ?? ""}
              onChange={(e) => set("category_id", e.target.value ? Number(e.target.value) : null)}
            >
              <option value="">(sin categoría)</option>
              {categories.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Precio">
            <Input type="number" step="0.01" value={form.price} onChange={(e) => set("price", Number(e.target.value))} />
          </Field>
          <Field label="Moneda">
            <Select value={form.currency} onChange={(e) => set("currency", e.target.value as Currency)}>
              <option value="USD">USD</option>
              <option value="CUP">CUP</option>
            </Select>
          </Field>
          <Field label="Comisión">
            <Input
              type="number"
              step="0.01"
              value={form.commission}
              onChange={(e) => set("commission", Number(e.target.value))}
            />
          </Field>
          <Field label="Moneda comisión">
            <Select
              value={form.commission_currency}
              onChange={(e) => set("commission_currency", e.target.value as Currency)}
            >
              <option value="USD">USD</option>
              <option value="CUP">CUP</option>
            </Select>
          </Field>
          <Field label="Stock">
            <Input
              type="number"
              value={form.stock ?? ""}
              onChange={(e) => set("stock", e.target.value === "" ? null : Number(e.target.value))}
            />
          </Field>
          <Field label="Estado">
            <Select value={form.status} onChange={(e) => set("status", e.target.value as ProductStatus)}>
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {STATUS_LABELS[s]}
                </option>
              ))}
            </Select>
          </Field>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={loading}>
            Cancelar
          </Button>
          <Button
            onClick={() => onSave(form)}
            disabled={!form.name.trim() || !form.supplier_id || loading}
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
