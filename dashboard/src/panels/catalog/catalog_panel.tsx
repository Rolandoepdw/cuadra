import { useState } from "react";
import { cn } from "@/lib/utils";
import { CategoriesTab } from "./categories_tab";
import { TcpTab } from "./tcp_tab";
import { ProductsTab } from "./products_tab";

const TABS = [
  { id: "products", label: "Productos" },
  { id: "tcp", label: "TCPs" },
  { id: "categories", label: "Categorías" },
] as const;

type TabId = (typeof TABS)[number]["id"];

export function CatalogPanel() {
  const [tab, setTab] = useState<TabId>("products");

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

      {tab === "products" && <ProductsTab />}
      {tab === "tcp" && <TcpTab />}
      {tab === "categories" && <CategoriesTab />}
    </div>
  );
}
