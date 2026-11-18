import type { ProductStatus, SaleStatus } from "@/types/api";

export function formatMoney(amount: number, currency: string): string {
  const n = new Intl.NumberFormat("es-CU", { maximumFractionDigits: 2 }).format(amount);
  return `${n} ${currency}`;
}

export function formatBytes(bytes: number): string {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const i = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
  return `${(bytes / Math.pow(1024, i)).toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

export function formatUptime(seconds: number): string {
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const parts: string[] = [];
  if (d) parts.push(`${d}d`);
  if (h) parts.push(`${h}h`);
  parts.push(`${m}m`);
  return parts.join(" ");
}

export function formatMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`;
}

export const STATUS_LABELS: Record<ProductStatus, string> = {
  ACTIVE: "Activo",
  OUT_OF_STOCK: "Agotado",
  PAUSED: "Pausado",
  REMOVED: "Eliminado",
};

export const STATUS_VARIANTS: Record<ProductStatus, "success" | "warning" | "secondary" | "destructive"> = {
  ACTIVE: "success",
  OUT_OF_STOCK: "warning",
  PAUSED: "secondary",
  REMOVED: "destructive",
};

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "—";
  const iso = value.includes("T") ? value : `${value.replace(" ", "T")}Z`;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("es-CU", {
    dateStyle: "short",
    timeStyle: "short",
    timeZone: "America/Havana",
  }).format(date);
}

export const GROUP_STATUS_LABELS = {
  ACTIVE: "Activo",
  PAUSED: "Inactivo",
  BLOCKED: "Bloqueado",
} as const;

export const BROADCAST_STATUS_LABELS = {
  SCHEDULED: "Programada",
  SENDING: "Enviando",
  COMPLETED: "Completada",
  FAILED: "Fallida",
} as const;

export const SALE_STATUS_LABELS: Record<SaleStatus, string> = {
  DRAFT: "Borrador",
  PENDING_APPROVAL: "Por aprobar",
  PENDING_DISPATCH: "Por despachar",
  DISPATCHED: "Despachada",
  CONFIRMED: "Confirmada por TCP",
  DELIVERED: "Entregada",
  SETTLED: "Comisión cobrada",
  CANCELLED: "Cancelada",
  NEEDS_OPERATOR: "Requiere operador",
};

export const SALE_STATUS_VARIANTS: Record<
  SaleStatus,
  "success" | "warning" | "secondary" | "destructive" | "outline"
> = {
  DRAFT: "outline",
  PENDING_APPROVAL: "warning",
  PENDING_DISPATCH: "warning",
  DISPATCHED: "secondary",
  CONFIRMED: "secondary",
  DELIVERED: "success",
  SETTLED: "success",
  CANCELLED: "destructive",
  NEEDS_OPERATOR: "destructive",
};
