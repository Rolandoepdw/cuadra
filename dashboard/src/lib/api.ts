import { clearPin, getPin } from "./pin";
import type {
  Category,
  CategoryIn,
  CategoryPatch,
  Health,
  LlmModel,
  LlmStatus,
  Product,
  ProductImage,
  ProductIn,
  ProductPatch,
  Summary,
  Supplier,
  SupplierIn,
  SupplierPatch,
} from "@/tipos/api";

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

export const listCategories = () => request<Category[]>("/api/categories");
export const createCategory = (body: CategoryIn) =>
  request<Category>("/api/categories", { method: "POST", body: JSON.stringify(body) });
export const updateCategory = (id: number, body: CategoryPatch) =>
  request<Category>(`/api/categories/${id}`, { method: "PATCH", body: JSON.stringify(body) });
export const deleteCategory = (id: number) =>
  request<{ ok: boolean }>(`/api/categories/${id}`, { method: "DELETE" });

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
export const getLlmModels = () => request<LlmModel[]>("/api/llm/models");
export const llmAction = (action: "start" | "stop" | "restart") =>
  request<LlmStatus>(`/api/llm/${action}`, { method: "POST" });
