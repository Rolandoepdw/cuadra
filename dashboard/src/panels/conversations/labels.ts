import type { ConversationStatus, CustomerKind } from "@/types/api";

export const CONVERSATION_STATUS_LABELS: Record<ConversationStatus, string> = {
  ACTIVE: "Activa",
  WAITING_TCP: "Esperando TCP",
  CLOSED: "Cerrada",
};

export const CONVERSATION_STATUS_VARIANTS: Record<
  ConversationStatus,
  "success" | "warning" | "secondary"
> = {
  ACTIVE: "success",
  WAITING_TCP: "warning",
  CLOSED: "secondary",
};

export const CUSTOMER_KIND_LABELS: Record<CustomerKind, string> = {
  customer: "Cliente",
  supplier: "Proveedor",
};

export const MESSAGE_AUTHOR_LABELS: Record<string, string> = {
  customer: "Cliente",
  bot: "Bot",
  tcp: "Proveedor",
  operator: "Operador",
};
