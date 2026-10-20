// TypeScript mirror of the dashboard API contracts (see cuadra_doc/02 + 001_init.sql).

export type Currency = "USD" | "CUP";
export type ProductStatus = "ACTIVE" | "OUT_OF_STOCK" | "PAUSED" | "REMOVED";

export interface Category {
  id: number;
  name: string;
  active: boolean;
}

export interface Supplier {
  id: number;
  name: string;
  business_alias: string | null;
  whatsapp_phone: string | null;
  telegram_id: string | null;
  delivery_zone: string | null;
  payment_terms: string | null;
  priority: number;
  notes: string | null;
  active: boolean;
}

export interface ProductImage {
  id: number;
  product_id: number;
  local_path: string;
  is_main: boolean;
  sort_order: number;
  url?: string;
}

export interface Product {
  id: number;
  supplier_id: number;
  category_id: number | null;
  name: string;
  description: string | null;
  price: number;
  currency: Currency;
  commission: number;
  commission_currency: Currency;
  stock: number | null;
  status: ProductStatus;
  specifications: Record<string, unknown>;
  images?: ProductImage[];
}

export interface Summary {
  messages_today: number;
  active_conversations: number;
  escalations_open: number;
  avg_llm_ms: number | null;
  avg_stt_ms: number | null;
  queue_size: number;
}

export interface Health {
  service: string;
  version: string;
  utc: string;
  uptime_s: number;
  database: { integrity: string; migrations: string[]; size_bytes: number };
  job_queue: number;
  llm: {
    provider: string;
    status: string;
    managed: boolean;
    running: boolean;
    model: string | null;
    url: string;
  };
  telegram_channel: string;
  debug: boolean;
}

export interface LlmModel {
  name: string;
  path: string;
  size_bytes: number;
}

export interface LlmStatus {
  managed: boolean;
  running: boolean;
  model: string | null;
  url: string;
  pid?: number | null;
}

// ---- request payloads ----

export interface CategoryIn {
  name: string;
  active?: boolean;
}

export interface SupplierIn {
  name: string;
  business_alias?: string | null;
  whatsapp_phone?: string | null;
  telegram_id?: string | null;
  delivery_zone?: string | null;
  payment_terms?: string | null;
  priority?: number;
  notes?: string | null;
  active?: boolean;
}

export interface ProductIn {
  supplier_id: number;
  category_id?: number | null;
  name: string;
  description?: string | null;
  price: number;
  currency?: Currency;
  commission: number;
  commission_currency?: Currency;
  stock?: number | null;
  status?: ProductStatus;
  specifications?: Record<string, unknown>;
}

export type CategoryPatch = Partial<CategoryIn>;
export type SupplierPatch = Partial<SupplierIn>;
export type ProductPatch = Partial<Omit<ProductIn, "supplier_id">>;
