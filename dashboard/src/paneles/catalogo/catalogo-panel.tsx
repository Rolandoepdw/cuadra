import { useState } from "react";
import { cn } from "@/lib/utils";
import { CategoriasTab } from "./categorias-tab";
import { TcpTab } from "./tcp-tab";
import { ProductsTab } from "./products-tab";

const TABS = [
  { id: "productos", label: "Productos" },
  { id: "tcp", label: "TCPs" },
  { id: "categorias", label: "Categorías" },
] as const;

type TabId = (typeof TABS)[number]["id"];

export function CatalogoPanel() {
  const [tab, setTab] = useState<TabId>("productos");

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Catálogo</h1>
        <p className="text-sm text-muted-foreground">Administra proveedores, productos y categorías.</p>
      </div>

      <div className="flex gap-1 border-b">
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={cn(
              "-mb-px border-b-2 px-4 py-2 text-sm font-medium transition-colors",
              tab === t.id
                ? "border-primary text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === "productos" && <ProductsTab />}
      {tab === "tcp" && <TcpTab />}
      {tab === "categorias" && <CategoriasTab />}
    </div>
  );
}
