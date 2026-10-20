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
    gpu?: GpuInfo | null;
    ctx?: number | null;
    ctx_default?: number | null;
    ctx_max?: number | null;
    memory?: LlmMemory | null;
    process_ram_mib?: number | null;
  };
  telegram_channel: string;
  debug: boolean;
}

export interface LlmModel {
  name: string;
  path: string;
  size_bytes: number;
  context_max?: number | null;
}

export interface LlmModelsResponse {
  models: LlmModel[];
  current: string | null;
}

export interface GpuInfo {
  name: string;
  total_mib: number;
  used_mib: number;
  free_mib: number;
}

export interface LlmMemory {
  model_vram_mib: number;
  model_ram_mib: number;
  model_total_mib: number;
  kv_vram_mib: number;
  kv_ram_mib: number;
  compute_vram_mib: number;
  compute_ram_mib: number;
  vram_total_mib: number;
  ram_total_mib: number;
  offload_pct: number | null;
  layers: { offloaded: number; total: number } | null;
}

export interface LlmStatus {
  provider?: string;
  managed: boolean;
  running: boolean;
  model: string | null;
  url: string;
  pid?: number | null;
  bin?: string;
  bin_present?: boolean;
  ctx?: number | null;
  ctx_default?: number | null;
  ctx_max?: number | null;
  gpu_layers?: number | null;
  port?: number | null;
  gpu?: GpuInfo | null;
  memory?: LlmMemory | null;
  process_ram_mib?: number | null;
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
