// TypeScript mirror of the dashboard API contracts (see cuadra_doc/02 + 001_init.sql).

export type Currency = "USD" | "CUP";
export type ProductStatus = "ACTIVE" | "OUT_OF_STOCK" | "PAUSED" | "REMOVED";
export type CommissionType = "FIXED" | "PERCENT";

export interface Category {
  id: number;
  name: string;
  active: boolean;
}

export interface Zone {
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
  telegram_peer_id: string | null;
  delivery_zone: string | null;
  delivery_zone_ids: number[];
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
  commission_type: CommissionType;
  stock: number | null;
  stock_reserved: number | null;
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
  pacing: PacingState;
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
  telegram_user: TelegramUserStatus;
  debug: boolean;
}

export interface PacingState {
  sender: string;
  min_gap_s: number;
  last_action_at: string | null;
  blocked_until: string | null;
  next_action_at: string;
  wait_s: number;
  reason: "ready" | "gap" | "cooldown";
}

export interface TelegramUserStatus {
  configured: boolean;
  connected: boolean;
  username: string | null;
  user_id: number | null;
  error: string | null;
  session_file: string;
  session_exists: boolean;
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

export interface ZoneIn {
  name: string;
  active?: boolean;
}

export interface SupplierIn {
  name: string;
  business_alias?: string | null;
  whatsapp_phone?: string | null;
  telegram_id?: string | null;
  telegram_peer_id?: string | null;
  delivery_zone?: string | null;
  delivery_zone_ids: number[];
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
  commission_type?: CommissionType;
  stock?: number | null;
  status?: ProductStatus;
  specifications?: Record<string, unknown>;
}

export type CategoryPatch = Partial<CategoryIn>;
export type ZonePatch = Partial<ZoneIn>;
export type SupplierPatch = Partial<SupplierIn>;
export type ProductPatch = Partial<Omit<ProductIn, "supplier_id">>;

// ---------------------------------------------------------------- broadcasts

export type BroadcastChannel = "telegram" | "whatsapp";
export type GroupStatus = "ACTIVE" | "PAUSED" | "BLOCKED";
export type Sender = "bot" | "userbot";
export type BroadcastStatus = "SCHEDULED" | "SENDING" | "COMPLETED" | "FAILED";

export interface BroadcastGroup {
  id: number;
  channel: BroadcastChannel;
  target: string;
  name: string;
  zone_ids: number[];
  sender: Sender;
  status: GroupStatus;
  max_posts_per_day: number;
  min_interval_s: number;
  max_interval_s: number;
  category_ids: number[];
  notes: string | null;
}

export interface BroadcastStrategy {
  max_posts_per_day: number;
  min_interval_s: number;
  max_interval_s: number;
  repeat_passes: number;
  repeat_window_days: number;
}

export interface BroadcastStrategyIn extends BroadcastStrategy {
  apply_to_groups?: boolean;
}

export type TransportPolicy = "coordinated" | "included" | "not_included";

export interface DeliverySettings {
  transport_policy: TransportPolicy;
}

export interface BroadcastReevaluate {
  checked: number;
  queued: number;
  waiting: number;
  failed: number;
  skipped: number;
  resumed: boolean;
}

export interface BroadcastPublishing {
  publishing: boolean;
  queued_dropped?: number;
}

export interface Broadcast {
  id: number;
  channel: BroadcastChannel;
  template: string;
  product_id: number | null;
  status: BroadcastStatus;
  enabled: boolean;
  next_send_at: string | null;
  posts_sent: number;
  posts_failed: number;
  group_count: number;
  group_ids: number[];
  created_at: string;
  updated_at: string;
}

export interface BroadcastPost {
  id: number;
  broadcast_id: number;
  group_id: number;
  channel: string;
  group_name: string;
  group_target: string;
  group_sender: Sender;
  sent_text: string;
  image_path: string | null;
  status: "QUEUED" | "SENT" | "FAILED";
  error: string | null;
  attempts: number;
  sent_at: string;
}

export interface BroadcastGroupIn {
  channel?: BroadcastChannel;
  target: string;
  name: string;
  zone_ids?: number[];
  sender?: Sender;
  status?: GroupStatus;
  max_posts_per_day?: number;
  min_interval_s?: number;
  max_interval_s?: number;
  category_ids?: number[];
  notes?: string | null;
}

export type BroadcastGroupPatch = Partial<BroadcastGroupIn>;

export interface BroadcastIn {
  channel?: BroadcastChannel;
  template: string;
  product_id?: number | null;
  group_ids?: number[];
}

// ---------------------------------------------------------------------- logs

export type LogCategory = "publications" | "conversations" | "entities" | "system";
export type LogSeverity = "info" | "success" | "warning" | "error";
export type LogWindow = "1h" | "24h" | "7d" | "all";

export interface LogEntry {
  id: number;
  created_at: string;
  category: LogCategory;
  source: string;
  type: string;
  label: string;
  summary: string;
  severity: LogSeverity;
  entity: string | null;
  entity_id: number | null;
  entity_label: string | null;
  details: Record<string, unknown>;
}

export interface LogList {
  items: LogEntry[];
  total: number;
  counts: Record<LogCategory, number>;
  category_labels: Record<LogCategory, string>;
  window: LogWindow;
  generated_at: string;
}

// -------------------------------------------------------------------- finance

export type SaleStatus =
  | "DRAFT"
  | "PENDING_APPROVAL"
  | "PENDING_DISPATCH"
  | "DISPATCHED"
  | "CONFIRMED"
  | "DELIVERED"
  | "SETTLED"
  | "CANCELLED"
  | "NEEDS_OPERATOR";

export type SalesMode = "assisted" | "hybrid" | "autonomous";
export type FollowupMode = "off" | "shadow" | "on";
export type LedgerStatus = "PENDING" | "COLLECTED" | "VOID";

export interface SalesSettings {
  mode: SalesMode;
}

export interface FollowupSettings {
  mode: FollowupMode;
}

export interface Sale {
  id: number;
  order_code: string | null;
  conversation_id: number | null;
  product_id: number;
  product_name: string | null;
  supplier_id: number;
  supplier_name: string | null;
  customer_id: number;
  quantity: number;
  amount: number;
  currency: Currency;
  commission_type: CommissionType;
  commission: number;
  commission_currency: Currency;
  recipient_name: string | null;
  delivery_zone: string | null;
  delivery_zone_id: number | null;
  delivery_address: string | null;
  customer_phone: string | null;
  status: SaleStatus;
  source: "bot" | "operator";
  notes: string | null;
  tcp_message_id: string | null;
  approved_at: string | null;
  dispatched_at: string | null;
  tcp_ack_at: string | null;
  delivered_at: string | null;
  settled_at: string | null;
  cancelled_at: string | null;
  created_at: string;
  updated_at: string;
  ledger_status: LedgerStatus | null;
  commission_earned: number | null;
  ledger_currency: Currency | null;
}

export interface SaleFollowup {
  id: number;
  order_code: string | null;
  product_id: number;
  product_name: string | null;
  supplier_id: number;
  supplier_name: string | null;
  recipient_name: string | null;
  amount: number;
  currency: Currency;
  status: SaleStatus;
  created_at: string;
  updated_at: string;
  age_hours: number;
  followup_sent_at: string | null;
  followup_muted_at: string | null;
}

export interface FollowupPage {
  hours: number;
  items: SaleFollowup[];
}

export interface CurrencyTotals {
  currency: Currency;
  gross_sales: number;
  commission_earned: number;
  commission_collected: number;
  commission_pending: number;
}

export interface FinanceSummary {
  mode: SalesMode;
  sales_today: number;
  sales_pending_approval: number;
  sales_pending_dispatch: number;
  sales_dispatched: number;
  sales_delivered_pending_settlement: number;
  sales_needs_operator: number;
  conversion_pending: number;
  totals_by_currency: CurrencyTotals[];
}

export interface LedgerEntry {
  id: number;
  sale_id: number;
  order_code: string | null;
  sale_status: SaleStatus | null;
  product_id: number;
  supplier_id: number;
  supplier_name: string | null;
  product_name: string;
  sale_total: number;
  currency: Currency;
  commission_type: CommissionType;
  commission_rate: number;
  commission_earned: number;
  commission_currency: Currency;
  supplier_payout: number | null;
  status: LedgerStatus;
  collected_at: string | null;
  notes: string | null;
  created_at: string;
}

export interface SalesPage {
  items: Sale[];
  total: number;
  limit: number;
  offset: number;
}

export interface LedgerPage {
  items: LedgerEntry[];
  total: number;
  limit: number;
  offset: number;
}

export interface CancelSaleIn {
  reason: string;
}

// ------------------------------------------------------------- execution traces
export type TraceEntry = {
  id: number;
  timestamp: string;
  method: string;
  input: any;
  output: any;
  ok: boolean;
  error: string | null;
  error_type: string | null;
  duration_ms: number | null;
  conversation_id: number | null;
  turn_id: number | null;
};

export interface TracesPage {
  items: TraceEntry[];
  total: number;
  limit: number;
  offset: number;
}

// ------------------------------------------------------------- metrics
export interface MetricsSummary {
  total_traces_today: number;
  failed_traces_today: number;
  avg_duration_ms: number | null;
  avg_llm_ms: number | null;
  stuck_sales_dispatched: number;
  open_escalations: number;
  ambiguous_acks_today: number;
  active_conversations: number;
}

export interface MetricsTimelinePoint {
  dia: string;
  total_traces: number;
  failed_traces: number;
  avg_duration_ms: number | null;
}

export interface MetricsTimeline {
  timeline: MetricsTimelinePoint[];
}

// ------------------------------------------------------------- catalog audit

export type MutationStatus = "APPLIED" | "PENDING_REVIEW" | "REVERTED" | "REJECTED";
export type MutationType = "PRICE" | "STOCK" | "STATUS" | "SPECS" | "ZONE" | "MIXED";
export type CorrelationMethod = "case_id" | "single_pending" | "heuristic" | "manual";

export interface CatalogState {
  price: number;
  currency: Currency;
  stock: number;
  stock_reserved: number;
  status: ProductStatus;
  specifications: Record<string, unknown>;
  delivery_zone_ids: number[];
  updated_at?: string | null;
}

export interface CatalogMutation {
  id: number;
  product_id: number;
  product_name: string | null;
  supplier_id: number;
  supplier_name: string | null;
  escalation_id: number | null;
  sale_id: number | null;
  source_message_id: number | null;
  source_text: string | null;
  mutation_type: MutationType;
  previous_state_json: string;
  new_state_json: string;
  previous_state: CatalogState;
  new_state: CatalogState;
  previous_updated_at: string | null;
  confidence: number;
  correlation_method: CorrelationMethod;
  ambiguous: boolean;
  status: MutationStatus;
  notes: string | null;
  applied_by: "bot" | "operator";
  reverted_at: string | null;
  reverted_by: string | null;
  revert_reason: string | null;
  created_at: string;
}

export interface CatalogMutationPage {
  items: CatalogMutation[];
  total: number;
  limit: number;
  offset: number;
}

export interface MutationReasonIn {
  reason: string;
}

// ------------------------------------------------------------- conversations
export type CustomerKind = "customer" | "supplier";
export type ConversationStatus = "ACTIVE" | "WAITING_TCP" | "CLOSED";
export type MessageDirection = "INBOUND" | "OUTBOUND";
export type MessageAuthor = "customer" | "bot" | "tcp" | "operator";

export interface ConversationCustomerSummary {
  id: number;
  name: string | null;
  handle: string | null;
  phone_masked: string | null;
}

export interface ConversationLastMessage {
  body: string | null;
  direction: MessageDirection;
  author: MessageAuthor;
  created_at: string;
}

export interface ConversationLastSale {
  id: number;
  order_code: string | null;
  status: SaleStatus;
  amount: number;
  currency: Currency;
  product_name: string | null;
}

export interface ConversationListItem {
  id: number;
  kind: CustomerKind;
  channel: string;
  identity: string;
  status: ConversationStatus;
  stage: string | null;
  customer: ConversationCustomerSummary;
  last_message: ConversationLastMessage | null;
  messages_count: number;
  turns_count: number;
  questions_count: number;
  open_escalations: number;
  last_sale: ConversationLastSale | null;
  needs_attention: boolean;
  created_at: string;
  updated_at: string;
  closed_at: string | null;
}

export interface ConversationsPage {
  items: ConversationListItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface ConversationsSummary {
  total: number;
  open: number;
  waiting_tcp: number;
  needs_attention: number;
  with_open_escalation: number;
  with_sale: number;
  customers: number;
  suppliers: number;
}

export interface ConversationMessage {
  id: number;
  direction: MessageDirection;
  author: MessageAuthor;
  type: string;
  body: string | null;
  file_path: string | null;
  latency_ms: number | null;
  created_at: string;
}

export interface ConversationMessagesPage {
  items: ConversationMessage[];
  has_more: boolean;
  limit: number;
}

export interface ConversationCustomerDetail {
  id: number;
  name: string | null;
  display_name: string | null;
  kind: CustomerKind;
  handle: string | null;
  phone_masked: string | null;
  usual_zone: string | null;
  usual_address_masked: string | null;
  notes: string | null;
  created_at: string;
}

export interface ConversationFact {
  fact_key: string;
  fact_value: string;
  confidence: number;
  updated_at: string | null;
}

export interface ConversationSale {
  id: number;
  order_code: string | null;
  status: SaleStatus;
  quantity: number;
  amount: number;
  currency: Currency;
  product_id: number;
  product_name: string | null;
  created_at: string;
  updated_at: string;
}

export interface ConversationEscalation {
  id: number;
  product_id: number | null;
  product_name: string | null;
  supplier_id: number;
  supplier_name: string | null;
  question: string;
  tcp_answer: string | null;
  status: string;
  created_at: string;
  answered_at: string | null;
  resolved_at: string | null;
}

export interface ConversationTurn {
  id: number;
  status: string;
  intent: string | null;
  product_id: number | null;
  product_name: string | null;
  stage: string | null;
  generator: string | null;
  escalated: boolean;
  sale_id: number | null;
  latency_ms: number | null;
  outcome: "ok" | "error";
  created_at: string;
  finished_at: string | null;
}

export interface ConversationTrace {
  id: number;
  method: string;
  ok: boolean;
  duration_ms: number | null;
  timestamp: string;
}

export interface ConversationDetail {
  conversation: ConversationListItem;
  customer: ConversationCustomerDetail;
  context: Record<string, unknown>;
  facts: ConversationFact[];
  messages: ConversationMessage[];
  messages_has_more: boolean;
  sales: ConversationSale[];
  escalations: ConversationEscalation[];
  turns: ConversationTurn[];
  traces: ConversationTrace[];
}

// ---------------------------------------------------------------- customers
export type CustomerStatus = "ACTIVE" | "BLOCKED" | "SPAM" | "MERGED";
export type CustomerSegment =
  | "bloqueado"
  | "riesgo"
  | "vip"
  | "comprador"
  | "potencial"
  | "poco_rentable"
  | "frio"
  | "nuevo";
export type CustomerRisk = "low" | "medium" | "high";

export interface CustomerChannelRef {
  channel: string;
  handle: string | null;
}

export interface CustomerListItem {
  id: number;
  name: string | null;
  display_name: string | null;
  kind: CustomerKind;
  status: CustomerStatus;
  phone_masked: string | null;
  zone: string | null;
  channels: CustomerChannelRef[];
  conversations_total: number;
  conversations_open: number;
  questions_total: number;
  turns_total: number;
  sales_created: number;
  sales_settled: number;
  revenue_usd: number;
  revenue_cup: number;
  commission_usd: number;
  commission_cup: number;
  conversion: number;
  effort_score: number;
  first_seen_at: string | null;
  last_seen_at: string | null;
  last_purchase_at: string | null;
  segment: CustomerSegment;
  risk: CustomerRisk;
  risk_score: number;
  tags: string[];
}

export interface CustomersPage {
  items: CustomerListItem[];
  total: number;
  limit: number;
  offset: number;
  segments: CustomerSegment[];
}

export interface CustomersSummary {
  total: number;
  new_7d: number;
  active_7d: number;
  buyers: number;
  conversion: number;
  risk: number;
  blocked: number;
  low_value: number;
  revenue_usd: number;
  revenue_cup: number;
  commission_usd: number;
  commission_cup: number;
}

export interface CustomerContact {
  id: number;
  kind: "phone" | "email" | "whatsapp";
  value_masked: string | null;
  normalized_masked: string | null;
  label: string | null;
  source: string;
  confidence: number;
  verified: boolean;
  first_seen_at: string | null;
  last_seen_at: string | null;
}

export interface CustomerNote {
  id: number;
  body: string;
  author: string | null;
  created_at: string;
}

export interface CustomerSignal {
  id: number;
  customer_id: number;
  kind: string;
  severity: "info" | "warning" | "critical";
  status: "OPEN" | "CONFIRMED" | "DISMISSED";
  count: number;
  evidence: Record<string, unknown>;
  first_seen_at: string | null;
  last_seen_at: string | null;
  resolved_at: string | null;
  resolved_by: string | null;
}

export interface CustomerMetrics {
  conversations_total: number;
  conversations_open: number;
  messages_in: number;
  messages_out: number;
  turns_total: number;
  questions_total: number;
  products_inquired: number;
  escalations_total: number;
  escalations_open: number;
  sales_created: number;
  sales_settled: number;
  sales_cancelled: number;
  sales_needs_operator: number;
  revenue_usd: number;
  revenue_cup: number;
  commission_usd: number;
  commission_cup: number;
  conversion: number;
  avg_turns_per_conversation: number;
  compute_s: number;
  effort_score: number;
  first_seen_at: string | null;
  last_seen_at: string | null;
  last_purchase_at: string | null;
  risk_score: number;
}

export interface CustomerConversationRef {
  id: number;
  channel: string;
  identity: string;
  handle: string | null;
  status: ConversationStatus;
  created_at: string;
  updated_at: string;
  closed_at: string | null;
  messages_count: number;
  last_message_at: string | null;
}

export interface CustomerDetail {
  customer: CustomerListItem & {
    address_masked: string | null;
    created_at: string;
  };
  metrics: CustomerMetrics;
  contacts: CustomerContact[];
  zones: { id: number; name: string }[];
  tags: string[];
  notes: CustomerNote[];
  signals: CustomerSignal[];
  conversations: CustomerConversationRef[];
  sales: ConversationSale[];
  merge: { merged_into_id: number | null };
}

export interface CustomerMergePreview {
  from: { id: number; name: string | null; display_name: string | null };
  into: { id: number; name: string | null; display_name: string | null };
  move: {
    conversations: number;
    messages: number;
    sales: number;
    contacts: number;
    tags: number;
    notes: number;
    signals: number;
  };
  conflicts: {
    contacts: { kind: string; normalized: string }[];
    tags: string[];
    open_signals: string[];
  };
  irreversible: boolean;
}

export interface CustomerReveal {
  customer_id: number;
  phone: string | null;
  address: string | null;
  contacts: {
    id: number;
    kind: string;
    value: string;
    normalized: string;
    source: string;
    verified: boolean;
  }[];
}

export interface CustomerPatch {
  name?: string;
  display_name?: string;
  status?: CustomerStatus;
  usual_zone_id?: number;
  usual_address?: string;
  phone?: string;
  notes?: string;
}
