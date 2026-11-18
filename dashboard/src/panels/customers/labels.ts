import type {
  CustomerRisk,
  CustomerSegment,
  CustomerStatus,
} from "@/types/api";

type BadgeVariant =
  | "default"
  | "secondary"
  | "destructive"
  | "outline"
  | "success"
  | "warning";

export const CUSTOMER_SEGMENT_LABELS: Record<CustomerSegment, string> = {
  bloqueado: "Bloqueado",
  riesgo: "Riesgo",
  vip: "VIP",
  comprador: "Comprador",
  potencial: "Potencial",
  poco_rentable: "Poco rentable",
  frio: "Frío",
  nuevo: "Nuevo",
};

export const CUSTOMER_SEGMENT_VARIANTS: Record<CustomerSegment, BadgeVariant> = {
  bloqueado: "destructive",
  riesgo: "destructive",
  vip: "success",
  comprador: "success",
  potencial: "secondary",
  poco_rentable: "warning",
  frio: "outline",
  nuevo: "outline",
};

export const CUSTOMER_RISK_LABELS: Record<CustomerRisk, string> = {
  low: "Bajo",
  medium: "Medio",
  high: "Alto",
};

export const CUSTOMER_RISK_VARIANTS: Record<CustomerRisk, BadgeVariant> = {
  low: "outline",
  medium: "warning",
  high: "destructive",
};

export const CUSTOMER_STATUS_LABELS: Record<CustomerStatus, string> = {
  ACTIVE: "Activo",
  BLOCKED: "Bloqueado",
  SPAM: "Spam",
  MERGED: "Fusionado",
};

export const CUSTOMER_STATUS_VARIANTS: Record<CustomerStatus, BadgeVariant> = {
  ACTIVE: "success",
  BLOCKED: "destructive",
  SPAM: "destructive",
  MERGED: "secondary",
};

export const SIGNAL_KIND_LABELS: Record<string, string> = {
  abuso_verbal: "Abuso verbal",
  pedido_falso: "Pedido falso",
  flood: "Flood",
  scraping_catalogo: "Scraping de catálogo",
  cancelaciones_repetidas: "Cancelaciones repetidas",
  reclamos_repetidos: "Reclamos repetidos",
  muchas_preguntas_sin_compra: "Muchas preguntas sin compra",
  posible_duplicado: "Posible duplicado",
};

export const SIGNAL_SEVERITY_LABELS: Record<string, string> = {
  info: "Info",
  warning: "Aviso",
  critical: "Crítico",
};

export const SIGNAL_SEVERITY_VARIANTS: Record<string, BadgeVariant> = {
  info: "outline",
  warning: "warning",
  critical: "destructive",
};
