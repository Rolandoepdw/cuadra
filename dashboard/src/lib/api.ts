import { clearPin, getPin } from "./pin";
import type {
  Broadcast,
  BroadcastGroup,
  BroadcastGroupIn,
  BroadcastGroupPatch,
  BroadcastIn,
  BroadcastPost,
  BroadcastPublishing,
  BroadcastReevaluate,
  BroadcastStrategy,
  BroadcastStrategyIn,
  CancelSaleIn,
  CatalogMutation,
  CatalogMutationPage,
  Category,
  CategoryIn,
  CategoryPatch,
  DeliverySettings,
  FinanceSummary,
  FollowupPage,
  Health,
  LedgerPage,
  LlmModelsResponse,
  LlmStatus,
  LogCategory,
  LogList,
  LogWindow,
  MutationReasonIn,
  Product,
  ProductImage,
  ProductIn,
  ProductPatch,
  Sale,
  SalesPage,
  SalesSettings,
  Summary,
  Supplier,
  SupplierIn,
  SupplierPatch,
  Zone,
  ZoneIn,
  ZonePatch,
} from "@/types/api";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const pin = getPin();
  if (pin) headers.set("X-PIN", pin);
  if (init.body && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }

  const res = await fetch(path, { ...init, headers });
  if (res.status === 401) {
    clearPin();
    throw new ApiError(401, "PIN inválido o ausente");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

// --------------------------------------------------------------- summary/health

export const getSummary = () => request<Summary>("/api/summary");
export const getHealth = () => request<Health>("/api/health");

// ------------------------------------------------------------------ categories

export const listCategories = (params?: { active?: boolean }) => {
  const qs = new URLSearchParams();
  if (params?.active) qs.set("active", "true");
  const suffix = qs.toString() ? `?${qs}` : "";
  return request<Category[]>(`/api/categories${suffix}`);
};
export const createCategory = (body: CategoryIn) =>
  request<Category>("/api/categories", { method: "POST", body: JSON.stringify(body) });
export const updateCategory = (id: number, body: CategoryPatch) =>
  request<Category>(`/api/categories/${id}`, { method: "PATCH", body: JSON.stringify(body) });
export const deleteCategory = (id: number) =>
  request<{ ok: boolean }>(`/api/categories/${id}`, { method: "DELETE" });

// ----------------------------------------------------------------------- zones

export const listZones = () => request<Zone[]>("/api/zones");
export const createZone = (body: ZoneIn) =>
  request<Zone>("/api/zones", { method: "POST", body: JSON.stringify(body) });
export const updateZone = (id: number, body: ZonePatch) =>
  request<Zone>(`/api/zones/${id}`, { method: "PATCH", body: JSON.stringify(body) });
// Zones are never deleted: the endpoint deactivates them (active = false).
export const deactivateZone = (id: number) =>
  request<Zone>(`/api/zones/${id}`, { method: "DELETE" });

// ------------------------------------------------------------------- suppliers

export const listSuppliers = () => request<Supplier[]>("/api/suppliers");
export const createSupplier = (body: SupplierIn) =>
  request<Supplier>("/api/suppliers", { method: "POST", body: JSON.stringify(body) });
export const updateSupplier = (id: number, body: SupplierPatch) =>
  request<Supplier>(`/api/suppliers/${id}`, { method: "PATCH", body: JSON.stringify(body) });
export const deleteSupplier = (id: number) =>
  request<Supplier>(`/api/suppliers/${id}`, { method: "DELETE" });

// -------------------------------------------------------------------- products

export const listProducts = (params: { supplier_id?: number; category_id?: number; status?: string; q?: string } = {}) => {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== "") qs.set(k, String(v));
  const suffix = qs.toString() ? `?${qs}` : "";
  return request<Product[]>(`/api/products${suffix}`);
};
export const getProduct = (id: number) => request<Product>(`/api/products/${id}`);
export const createProduct = (body: ProductIn) =>
  request<Product>("/api/products", { method: "POST", body: JSON.stringify(body) });
export const updateProduct = (id: number, body: ProductPatch) =>
  request<Product>(`/api/products/${id}`, { method: "PATCH", body: JSON.stringify(body) });
export const deleteProduct = (id: number) =>
  request<Product>(`/api/products/${id}`, { method: "DELETE" });

// ---------------------------------------------------------------------- images

export const uploadImage = (productId: number, file: File) => {
  const form = new FormData();
  form.append("file", file);
  return request<ProductImage>(`/api/products/${productId}/images`, { method: "POST", body: form });
};
export const setMainImage = (productId: number, imageId: number) =>
  request<ProductImage>(`/api/products/${productId}/main-image`, {
    method: "PUT",
    body: JSON.stringify({ image_id: imageId }),
  });
export const deleteImage = (productId: number, imageId: number) =>
  request<{ ok: boolean }>(`/api/products/${productId}/images/${imageId}`, { method: "DELETE" });

// ------------------------------------------------------------------------- llm

export const getLlmStatus = () => request<LlmStatus>("/api/llm/status");
export const getLlmModels = () => request<LlmModelsResponse>("/api/llm/models");
export const llmAction = (action: "start" | "stop" | "restart", model?: string) =>
  request<LlmStatus>(`/api/llm/${action}`, {
    method: "POST",
    body: model ? JSON.stringify({ model }) : undefined,
  });
export const setLlmModel = (model: string) =>
  request<LlmStatus>("/api/llm/model", { method: "POST", body: JSON.stringify({ model }) });
export const setLlmCtx = (ctx: number) =>
  request<LlmStatus>("/api/llm/ctx", { method: "POST", body: JSON.stringify({ ctx }) });


// ------------------------------------------------------------------ broadcasts

export const listBroadcastGroups = (params: { channel?: string; status?: string } = {}) => {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v) qs.set(k, v);
  const suffix = qs.toString() ? `?${qs}` : "";
  return request<BroadcastGroup[]>(`/api/broadcast-groups${suffix}`);
};
export const createBroadcastGroup = (body: BroadcastGroupIn) =>
  request<BroadcastGroup>("/api/broadcast-groups", { method: "POST", body: JSON.stringify(body) });
export const updateBroadcastGroup = (id: number, body: BroadcastGroupPatch) =>
  request<BroadcastGroup>(`/api/broadcast-groups/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
export const deactivateBroadcastGroup = (id: number) =>
  request<BroadcastGroup>(`/api/broadcast-groups/${id}`, { method: "DELETE" });

export const listBroadcasts = () => request<Broadcast[]>("/api/broadcasts");
export const createBroadcast = (body: BroadcastIn) =>
  request<Broadcast>("/api/broadcasts", { method: "POST", body: JSON.stringify(body) });
export const setBroadcastEnabled = (id: number, enabled: boolean) =>
  request<Broadcast>(`/api/broadcasts/${id}`, {
    method: "PATCH",
    body: JSON.stringify({ enabled }),
  });
export const getBroadcastPublishing = () =>
  request<BroadcastPublishing>("/api/broadcasts/publishing");
export const setBroadcastPublishing = (publishing: boolean) =>
  request<BroadcastPublishing>("/api/broadcasts/publishing", {
    method: "PUT",
    body: JSON.stringify({ publishing }),
  });
export const sendBroadcastNow = (id: number) =>
  request<Broadcast>(`/api/broadcasts/${id}/send-now`, { method: "POST" });
export const updateBroadcast = (id: number, body: BroadcastIn) => {
  const payload: BroadcastIn = {
    channel: body.channel,
    template: body.template,
    product_id: body.product_id ?? null,
    group_ids: body.group_ids ?? [],
  };
  return request<Broadcast>(`/api/broadcasts/${id}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
};
export const deleteBroadcast = (id: number) =>
  request<{ ok: boolean }>(`/api/broadcasts/${id}`, { method: "DELETE" });
export const reevaluateBroadcasts = () =>
  request<BroadcastReevaluate>("/api/broadcasts/reevaluate", { method: "POST" });
export const listBroadcastPosts = (id: number) =>
  request<BroadcastPost[]>(`/api/broadcasts/${id}/posts`);

export const getBroadcastStrategy = () =>
  request<BroadcastStrategy>("/api/settings/broadcast");

// ------------------------------------------------------------------------ logs

export const listLogs = (
  params: {
    category?: LogCategory;
    search?: string;
    window?: LogWindow;
    limit?: number;
    offset?: number;
  } = {},
) => {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== "") qs.set(k, String(v));
  }
  const suffix = qs.toString() ? `?${qs}` : "";
  return request<LogList>(`/api/logs${suffix}`);
};

export const setBroadcastStrategy = (body: BroadcastStrategyIn) =>
  request<BroadcastStrategy & { groups?: number }>(
    `/api/settings/broadcast?apply_to_groups=${body.apply_to_groups ? "true" : "false"}`,
    {
      method: "PUT",
      body: JSON.stringify({
        max_posts_per_day: body.max_posts_per_day,
        min_interval_s: body.min_interval_s,
        max_interval_s: body.max_interval_s,
        repeat_passes: body.repeat_passes,
        repeat_window_days: body.repeat_window_days,
      }),
    },
  );

export const getDeliverySettings = () =>
  request<DeliverySettings>("/api/settings/delivery");

export const setDeliverySettings = (body: DeliverySettings) =>
  request<DeliverySettings>("/api/settings/delivery", {
    method: "PUT",
    body: JSON.stringify(body),
  });

export const getSalesSettings = () =>
  request<SalesSettings>("/api/settings/sales");

export const setSalesSettings = (body: SalesSettings) =>
  request<SalesSettings>("/api/settings/sales", {
    method: "PUT",
    body: JSON.stringify(body),
  });

// -------------------------------------------------------------------- finance

export const getFinanceSummary = () => request<FinanceSummary>("/api/finance/summary");

export const listSales = (
  params: { status?: string; currency?: string; supplier_id?: number; limit?: number; offset?: number } = {},
) => {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== "") qs.set(k, String(v));
  }
  const suffix = qs.toString() ? `?${qs}` : "";
  return request<SalesPage>(`/api/finance/sales${suffix}`);
};

export const listFollowups = (hours = 24, limit = 50) =>
  request<FollowupPage>(`/api/finance/followups?hours=${hours}&limit=${limit}`);

export const listLedger = (
  params: { status?: string; currency?: string; supplier_id?: number; limit?: number; offset?: number } = {},
) => {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== "") qs.set(k, String(v));
  }
  const suffix = qs.toString() ? `?${qs}` : "";
  return request<LedgerPage>(`/api/finance/ledger${suffix}`);
};

export const approveSale = (id: number) =>
  request<Sale>(`/api/finance/approve/${id}`, { method: "POST" });

export const dispatchSale = (id: number) =>
  request<Sale>(`/api/finance/dispatch/${id}`, { method: "POST" });

export const settleSale = (id: number) =>
  request<Sale>(`/api/finance/settle/${id}`, { method: "POST" });

export const cancelSale = (id: number, reason: string) =>
  request<Sale>(`/api/finance/cancel/${id}`, {
    method: "POST",
    body: JSON.stringify({ reason } satisfies CancelSaleIn),
  });

// ------------------------------------------------------------- catalog audit

export const listCatalogMutations = (
  params: {
    status?: string;
    supplier_id?: number;
    product_id?: number;
    ambiguous?: boolean;
    date_from?: string;
    date_to?: string;
    limit?: number;
    offset?: number;
  } = {},
) => {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== "" && v !== null) qs.set(k, String(v));
  }
  const suffix = qs.toString() ? `?${qs}` : "";
  return request<CatalogMutationPage>(`/api/catalog/mutations${suffix}`);
};

export const approveCatalogMutation = (id: number) =>
  request<CatalogMutation>(`/api/catalog/mutations/${id}/approve`, {
    method: "POST",
  });

export const rejectCatalogMutation = (id: number, reason = "") =>
  request<CatalogMutation>(`/api/catalog/mutations/${id}/reject`, {
    method: "POST",
    body: JSON.stringify({ reason } satisfies MutationReasonIn),
  });

export const revertCatalogMutation = (id: number, reason = "") =>
  request<CatalogMutation>(`/api/catalog/mutations/${id}/revert`, {
    method: "POST",
    body: JSON.stringify({ reason } satisfies MutationReasonIn),
  });
