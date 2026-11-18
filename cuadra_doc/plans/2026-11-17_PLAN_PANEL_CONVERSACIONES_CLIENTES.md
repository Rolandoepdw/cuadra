# Plan — Paneles de Conversaciones y Clientes

**Fecha:** 2026-11-17
**Estado:** propuesta para aprobación (nada implementado todavía)
**Origen:** conversación con el operador del 2026-11-17.
**Base:** migraciones 001–019, pipeline de turnos, panel de Trazas, ventas/comisiones y dashboard React actual.

---

## 0. Resumen en una página

Hoy el dashboard **no tiene panel de Conversaciones ni de Clientes**. El único acceso es `GET /api/debug/conversation/{id}`, que exige `CUADRA_DEBUG=1` y no tiene listado, filtros ni métricas. La tabla `customers` es mínima (nombre, `preferences_json`, notas) y el intent de cada turno solo vive en `traces`, con retención de 30 días.

Este plan propone dos paneles y una base de datos pequeña que los sostiene:

1. **Conversaciones** — observabilidad operativa de cada chat (cliente o TCP): estado, etapa, último mensaje, escalaciones abiertas, venta en curso, transcripción, contexto y trazas. **Solo lectura + acciones administrativas** (cerrar, etiquetar, bloquear). **No se envían mensajes manuales desde el panel en v1.**
2. **Clientes** — identidad unificada, teléfonos, zonas y direcciones usadas, historial de conversaciones y ventas, métricas de esfuerzo/resultado, segmentos (comprador, potencial, poco rentable, frío) y **señales de riesgo/abuso con revisión humana**.

La piedra angular es una migración aditiva (`020`) que guarda el **resultado de cada turno** (intent, producto, etapa, escalación, venta, latencia) en `turns`. Sin eso, cualquier métrica de cliente obliga a releer `traces` (30 días) o a inventar números.

**Estimación:** 5–6 días de trabajo en 5 fases, cada una entregable por separado.

---

## 1. Estado actual (verificado en el código)

| Pieza | Hoy | Implicación |
|---|---|---|
| `customers` | `id`, `name`, `preferences_json`, `notes`, `created_at` | No hay teléfono, zona, estado, primera/última actividad ni segmento. |
| `customer_channels` | `(channel, identifier)` único por cliente | Es la identidad real de chat; no se toca. |
| `conversations` | `status`, `context_json`, `identity`, `closed_at` | El `stage` vive dentro del JSON; no es consultable directamente. |
| `messages` | Transcripción completa, `author`, `type`, `external_id`, `latency_ms` | Base del detalle y de la búsqueda. |
| `conversation_facts` | Hechos por conversación (producto, zona, nombre, teléfono, dirección) | Se pierden entre conversaciones cerradas; hay que promoverlos al cliente. |
| `sales` | `customer_phone`, `delivery_zone_id`, `delivery_address`, `recipient_name`, importe, comisión, estado | Ya contiene los mejores datos de contacto, pero dispersos por venta. |
| `turns` | `status`, `message_ids_json`, `due_at`, `job_id` | **No guarda intent, producto ni resultado.** |
| `traces` | Intent por turno, PII enmascarada, retención 30 días | Sirve para depurar, no para métricas de vida del cliente. |
| API | Sin `/api/conversations` ni `/api/customers` | Todo el backend de los paneles está por hacer. |
| Dashboard | React + Vite + TanStack Query/Table + PIN | Patrón claro para añadir paneles y tablas. |
| Decisión previa | “Nada de operación manual de conversaciones” (00_decisiones §7) | El panel nace de lectura; las acciones son administrativas, no de envío. |

**Dos problemas de identidad que el plan resuelve:**

- Un mismo cliente puede escribir al bot y al userbot y quedar como **dos filas** en `customers` (identificadores de Telegram distintos). El teléfono capturado en una venta es la clave para detectarlo, no para fusionarlo automáticamente.
- Las conversaciones de **TCP** se guardan como `customers` (con el nombre del proveedor). Si no se separan, las métricas de clientes incluirían proveedores. Se añade `customers.kind = 'customer' | 'supplier'`.

---

## 2. Principios de diseño

1. **Anti-ban primero.** Ningún botón del panel envía mensajes en v1. “Bloquear” significa que el bot deja de responder y de llamar al LLM; no se envía un aviso automático al bloqueado.
2. **PII mínima y enmascarada.** Se guarda lo necesario para operar (teléfono, zona, dirección), se muestra enmascarado por defecto y **revelar es una acción auditada**.
3. **Revisión humana.** Las señales de riesgo/abuso no bloquean ni penalizan solas: se muestran con evidencia y el operador confirma o descarta. El bot nunca insulta, cobra ni castiga.
4. **Métricas honestas.** No se inventa un costo en dólares. “Poco rentable” se mide como **esfuerzo** (turnos, mensajes, escalaciones, tiempo de cómputo) frente a **resultado** (ventas liquidadas y comisión cobrada). El costo monetario es opcional y configurable.
5. **Compatibilidad.** Todo es aditivo: no se rompe `preferences_json`, `customer_channels`, el pipeline ni las migraciones existentes.
6. **Un panel, una pregunta.** Conversaciones responde “¿qué está pasando en este chat?”. Clientes responde “¿quién es esta persona y nos conviene?”. Se enlazan, no se duplican.

---

## 3. Fase 0 — Base: resultado del turno (migración 020)

### 3.1 Por qué

Para el timeline de conversaciones y para las métricas de cliente se necesita, por turno: intención, producto, etapa, si escaló, si generó venta y cuánto tardó. Hoy solo existe en `traces` (30 días, JSON pesado). Persistirlo en `turns` cuesta un `UPDATE` por turno.

### 3.2 Esquema

`020_conversation_panel.sql`:

```sql
ALTER TABLE customers ADD COLUMN kind TEXT NOT NULL DEFAULT 'customer'
    CHECK (kind IN ('customer', 'supplier'));

ALTER TABLE turns ADD COLUMN intent TEXT;
ALTER TABLE turns ADD COLUMN product_id INTEGER REFERENCES products(id);
ALTER TABLE turns ADD COLUMN stage TEXT;
ALTER TABLE turns ADD COLUMN generator TEXT;
ALTER TABLE turns ADD COLUMN escalated INTEGER NOT NULL DEFAULT 0;
ALTER TABLE turns ADD COLUMN sale_id INTEGER REFERENCES sales(id);
ALTER TABLE turns ADD COLUMN reply_message_id INTEGER REFERENCES messages(id);
ALTER TABLE turns ADD COLUMN latency_ms INTEGER;
ALTER TABLE turns ADD COLUMN intent_ms INTEGER;
ALTER TABLE turns ADD COLUMN compose_ms INTEGER;
ALTER TABLE turns ADD COLUMN context_tokens INTEGER;
ALTER TABLE turns ADD COLUMN outcome TEXT NOT NULL DEFAULT 'ok'
    CHECK (outcome IN ('ok', 'error'));
ALTER TABLE turns ADD COLUMN error TEXT;
ALTER TABLE turns ADD COLUMN finished_at TEXT;

CREATE INDEX idx_turns_intent ON turns(intent);
CREATE INDEX idx_turns_product ON turns(product_id);
CREATE INDEX idx_turns_sale ON turns(sale_id);
CREATE INDEX idx_customers_kind ON customers(kind);
```

`customers.kind` se rellena en la misma migración cruzando `customer_channels.identifier` con `suppliers.telegram_id` / `suppliers.telegram_peer_id`; `ingest.ingest_tcp_message` lo fija a `supplier` al crear el cliente.

### 3.3 Escritura

En `pipeline.handle_turn`, después de `_run_turn` y antes de devolver:

- `intent` (texto), `product_id`, `stage`, `generator`, `escalated`, `sale_id`, `reply_message_id`, `latency_ms`, `intent_ms`, `compose_ms`, `context_tokens`, `finished_at`.
- Si `_run_turn` lanza excepción: `outcome='error'`, `error=<mensaje>`, `finished_at`.

Una función `turns.record_outcome(conn, turn_id, **fields)` evita repetir SQL. Es un `UPDATE` y no bloquea el turno.

### 3.4 Backfill (una vez)

`backend/scripts/backfill_turn_outcomes.py` (idempotente, `--dry-run`):

- Lee `traces` con `method IN ('pipeline._classify_light', 'pipeline._resolve_product', 'pipeline.handle_turn')` y `turn_id IS NOT NULL`.
- Rellena solo columnas `NULL`.
- Reporta cuántos turnos quedaron sin datos (los previos a la traza).

### 3.5 Criterios de aceptación

- Todo turno nuevo tiene `intent`, `product_id`, `stage`, `escalated`, `generator`, `latency_ms`.
- Un turno fallido deja `outcome='error'` y la traza sigue existiendo.
- El backfill no modifica filas ya completas y se puede correr dos veces.

---

## 4. Panel de Conversaciones

### 4.1 Alcance

**Incluye**

- Listado con filtros, búsqueda y paginación.
- Detalle con transcripción, contexto, hechos, ventas, escalaciones y trazas recientes.
- Indicador de “requiere atención”.
- Acciones administrativas: cerrar conversación, etiquetar/notas al cliente, bloquear (enlaza al panel de Clientes).
- Enlaces cruzados: conversación → cliente; conversación → venta; conversación → trazas.

**No incluye (v1)**

- Enviar mensajes manualmente (decisión anti-ban y §7 de `00_decisiones`).
- Editar o borrar mensajes.
- WebSockets: refresco cada 10 s es suficiente y no añade superficie.

### 4.2 API

Router nuevo `api/routes_conversations.py`, prefijo `/api`, PIN obligatorio.

| Método | Ruta | Uso |
|---|---|---|
| `GET` | `/api/conversations` | Listado paginado con filtros. |
| `GET` | `/api/conversations/summary` | KPIs de cabecera. |
| `GET` | `/api/conversations/{id}` | Detalle completo. |
| `GET` | `/api/conversations/{id}/messages?before_id=&limit=` | Paginación del historial. |
| `POST` | `/api/conversations/{id}/close` | Cierra y cancela turnos abiertos. |

**Filtros del listado:** `kind` (`customer`/`supplier`), `status`, `identity`, `channel`, `stage`, `needs_attention`, `has_escalation`, `has_sale`, `q` (nombre, identificador, teléfono normalizado, orden `V-000001` o texto de mensaje), `from`, `to`, `sort` (`updated_at` por defecto), `limit` (máx. 100), `offset`.

**Item del listado:**

```json
{
  "id": 1,
  "kind": "customer",
  "channel": "telegram",
  "identity": "userbot",
  "status": "ACTIVE",
  "stage": "QUOTE",
  "customer": {"id": 1, "name": "Rolando", "handle": "@rolandoepd", "phone_masked": "***-***-45"},
  "last_message": {"body": "¿y garantía?", "direction": "INBOUND", "author": "customer", "created_at": "..."},
  "messages_count": 42,
  "turns_count": 21,
  "questions_count": 12,
  "open_escalations": 1,
  "last_sale": {"order_code": "V-000001", "status": "SETTLED", "amount": 1440, "currency": "USD"},
  "needs_attention": true,
  "created_at": "...",
  "updated_at": "...",
  "closed_at": null
}
```

**Reglas:**

- `needs_attention` = `status='WAITING_TCP'` **o** escalación abierta **o** venta `NEEDS_OPERATOR` **o** hay un inbound posterior al último outbound.
- `questions_count` = turnos con `intent IN ('consulta_precio','consulta_stock','consulta_dato','comparar')`.
- El listado **nunca** devuelve teléfono o dirección completos: solo `phone_masked`.
- `q` por texto de mensaje usa `LIKE` en v1. Si el volumen lo exige, se añade `messages_fts` (FTS5) sin cambiar el contrato.

**Detalle:**

```json
{
  "conversation": {...},
  "customer": {...},
  "context": {"stage": "...", "product_id": 2, "sale_id": 1, "pending_offer": {...}, "summary": "..."},
  "facts": [{"fact_key": "zone_name", "fact_value": "La Lisa", "updated_at": "..."}],
  "messages": [...],
  "sales": [...],
  "escalations": [...],
  "turns": [...],
  "traces": [{"id": 161, "method": "pipeline.handle_turn", "ok": true, "duration_ms": 2400}]
}
```

El `context` se devuelve **saneado**: sin claves internas de control y con PII enmascarada. El detalle incluye `messages` paginados (por defecto últimos 100) y `has_more`.

### 4.3 UI

- Ruta `/conversations`, entrada en el sidebar con icono `MessagesSquare`.
- `dashboard/src/panels/conversations/conversations_panel.tsx` (listado + filtros + KPIs).
- `conversation_detail.tsx` (cabecera, transcripción, panel derecho con pestañas).
- `message_bubble.tsx` (inbound/outbound, bot/tcp/operator, audio/imagen con enlace, latencia, hora).
- `conversation_filters.tsx`.

**Listado:** tabla con Cliente, Canal/Identidad, Estado, Etapa, Último mensaje, Mensajes, Preguntas, Escalaciones, Venta, Actualizado. Fila clicable. Refresco 10 s con `placeholderData` para no parpadear.

**Detalle:** cabecera con cliente, canal, identidad, estado, etapa, badges de escalación/venta y botón “Ver cliente”. Transcripción con burbujas. Pestañas: **Contexto** (resumen, hechos, oferta pendiente, producto), **Ventas** (código, estado, importe), **Escalaciones** (pregunta, estado, respuesta), **Trazas** (últimas 20, enlace al panel de Trazas).

**Móvil:** el listado ocupa la pantalla; el detalle es una ruta completa (`/conversations/:id`) para poder compartir enlace y volver con el gesto del navegador.

### 4.4 Criterios de aceptación

- Encontrar una conversación por teléfono, nombre u orden en menos de 2 s.
- Ver la transcripción completa y entender qué generó cada respuesta (etapa, producto, generador).
- Ver escalaciones abiertas y ventas con su código.
- El listado no expone PII completa.
- Cerrar una conversación cancela sus turnos abiertos y no permite que un job viejo responda después.
- Tests de API: filtros, paginación, detalle, cierre, enmascarado y `needs_attention`.

---

## 5. Panel de Clientes

### 5.1 Modelo de identidad

- `customer_channels` sigue siendo la identidad canónica de chat (telegram/whatsapp).
- `customer_contacts` añade **teléfonos y correos** (y en el futuro WhatsApp), con normalización.
- `customers.kind` separa clientes de proveedores.
- Un cliente puede tener N conversaciones (el cierre por inactividad ya crea una nueva).
- **Nunca se fusiona automáticamente por teléfono.** Un teléfono compartido puede ser familia, negocio o suplantación. Se genera una señal `possible_duplicate` y el operador decide.

**Puntos de captura:**

| Momento | Qué se guarda |
|---|---|
| Inbound de cualquier canal | `last_seen_at`, contacto de canal. |
| `memory.capture_facts` | nombre, teléfono, zona, dirección (hechos de la conversación). |
| Creación/actualización de venta | teléfono, zona, dirección y destinatario reales de la venta. |
| Operador en el panel | contacto, etiqueta, nota, zona habitual. |

**Normalización:** `sales.normalize_phone` (6–8 dígitos, `+53` opcional). El valor mostrado y el normalizado se guardan por separado.

### 5.2 Esquema (migración 021)

`021_customer_panel.sql`:

```sql
ALTER TABLE customers ADD COLUMN display_name TEXT;
ALTER TABLE customers ADD COLUMN phone TEXT;
ALTER TABLE customers ADD COLUMN phone_normalized TEXT;
ALTER TABLE customers ADD COLUMN usual_zone_id INTEGER REFERENCES zones(id);
ALTER TABLE customers ADD COLUMN usual_address TEXT;
ALTER TABLE customers ADD COLUMN status TEXT NOT NULL DEFAULT 'ACTIVE'
    CHECK (status IN ('ACTIVE', 'BLOCKED', 'SPAM', 'MERGED'));
ALTER TABLE customers ADD COLUMN merged_into_id INTEGER REFERENCES customers(id);
ALTER TABLE customers ADD COLUMN first_seen_at TEXT;
ALTER TABLE customers ADD COLUMN last_seen_at TEXT;

CREATE TABLE customer_contacts (
    id            INTEGER PRIMARY KEY,
    customer_id   INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL CHECK (kind IN ('phone', 'email', 'whatsapp')),
    value         TEXT NOT NULL,
    normalized    TEXT NOT NULL,
    label         TEXT,
    source        TEXT NOT NULL,          -- channel | sale | fact | operator
    confidence    REAL NOT NULL DEFAULT 1.0,
    verified      INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT DEFAULT (datetime('now')),
    last_seen_at  TEXT DEFAULT (datetime('now')),
    UNIQUE (kind, normalized)
);
CREATE INDEX idx_customer_contacts_customer ON customer_contacts(customer_id);
CREATE INDEX idx_customer_contacts_normalized ON customer_contacts(normalized);

CREATE TABLE customer_tags (
    id          INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    tag         TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'manual',   -- manual | auto
    reason      TEXT,
    created_by  TEXT,
    created_at  TEXT DEFAULT (datetime('now')),
    UNIQUE (customer_id, tag)
);

CREATE TABLE customer_notes (
    id          INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    body        TEXT NOT NULL,
    author      TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE customer_signals (
    id             INTEGER PRIMARY KEY,
    customer_id    INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL,
    severity       TEXT NOT NULL DEFAULT 'info'
                   CHECK (severity IN ('info', 'warning', 'critical')),
    status         TEXT NOT NULL DEFAULT 'OPEN'
                   CHECK (status IN ('OPEN', 'CONFIRMED', 'DISMISSED')),
    count          INTEGER NOT NULL DEFAULT 1,
    evidence_json  TEXT,
    first_seen_at  TEXT DEFAULT (datetime('now')),
    last_seen_at   TEXT DEFAULT (datetime('now')),
    resolved_at    TEXT,
    resolved_by    TEXT
);
CREATE UNIQUE INDEX idx_customer_signals_open
    ON customer_signals(customer_id, kind) WHERE status = 'OPEN';
CREATE INDEX idx_customer_signals_status ON customer_signals(status, severity);

CREATE TABLE customer_merges (
    id                INTEGER PRIMARY KEY,
    from_customer_id  INTEGER NOT NULL REFERENCES customers(id),
    into_customer_id  INTEGER NOT NULL REFERENCES customers(id),
    reason            TEXT,
    created_by        TEXT,
    created_at        TEXT DEFAULT (datetime('now'))
);

CREATE INDEX idx_customers_last_seen ON customers(last_seen_at);
CREATE INDEX idx_customers_status ON customers(status);
```

**Compatibilidad:** `preferences_json` se sigue leyendo; las escrituras nuevas van a columnas y `customer_contacts`. `customer_profile` sigue funcionando y se amplía.

### 5.3 Métricas (definiciones explícitas)

Se calculan por cliente, **solo sobre `kind='customer'`**, y separando USD y CUP (nunca se suman monedas).

| Métrica | Definición | Fuente |
|---|---|---|
| `conversations_total` | Conversaciones del cliente | `conversations` |
| `conversations_open` | `ACTIVE` o `WAITING_TCP` | `conversations` |
| `messages_in` / `messages_out` | Mensajes por dirección | `messages` |
| `turns_total` | Turnos con `outcome='ok'` | `turns` |
| `questions_total` | Turnos con intent de consulta (`consulta_precio`, `consulta_stock`, `consulta_dato`, `comparar`) | `turns.intent` |
| `products_inquired` | Productos distintos consultados | `turns.product_id` |
| `escalations_total` / `open` | Escalaciones | `escalations` |
| `sales_created` | Ventas no canceladas | `sales` |
| `sales_settled` | Ventas `SETTLED` | `sales` |
| `sales_cancelled` | Ventas `CANCELLED` | `sales` |
| `revenue_usd` / `revenue_cup` | Suma de importes no cancelados | `sales` |
| `commission_usd` / `commission_cup` | Comisión cobrada (`COLLECTED`) | `commission_ledger` |
| `conversion` | `sales_settled / conversations_total` | derivada |
| `avg_turns_per_conversation` | `turns_total / conversations_total` | derivada |
| `compute_s` | Suma de `(intent_ms + compose_ms)/1000` | `turns` |
| `first_seen_at` / `last_seen_at` | Primera/última actividad | `customers` |
| `last_purchase_at` | Última venta no cancelada | `sales` |
| `effort_score` | `turns_total + 2*escalations_total + messages_in/2` (pesos configurables) | derivada |

**Sobre “rentabilidad”:** no se convierte a dólares sin un costo real. El panel muestra **esfuerzo** y **resultado** por separado, y el segmento se deriva de ambos. Si más adelante se quiere costo monetario, se añade `customers.cost_per_compute_minute` en `settings` y se calcula; no se inventa ahora.

**Rendimiento:** en v1 se calcula con SQL agregado y paginación (cientos de clientes es trivial). Si el listado supera ~200 ms p95, se añade una tabla `customer_metrics` refrescada por el scheduler; el contrato de la API no cambia.

### 5.4 Segmentos

Umbrales en `settings` (editables sin tocar código):

```
customers.segment.vip_min_settled        = 3
customers.segment.vip_days               = 90
customers.segment.potential_days         = 30
customers.segment.cold_days              = 90
customers.segment.low_value_min_turns    = 20
customers.segment.low_value_min_conversations = 3
customers.risk.medium                    = 30
customers.risk.high                      = 60
```

Reglas, evaluadas en orden:

| Segmento | Regla | Lectura |
|---|---|---|
| `bloqueado` | `status IN ('BLOCKED','SPAM')` | No recibe respuestas. |
| `riesgo` | `risk_score >= high` con señales **confirmadas** | Revisar antes de operar. |
| `vip` | `sales_settled >= vip_min_settled` y última compra ≤ `vip_days` | Cliente recurrente de alto valor. |
| `comprador` | `sales_settled >= 1` | Ya compró al menos una vez. |
| `potencial` | 0 ventas liquidadas, al menos una consulta, última actividad ≤ `potential_days` | Interés real reciente. |
| `poco_rentable` | 0 ventas liquidadas y (`turns_total >= low_value_min_turns` o `conversations_total >= low_value_min_conversations`), activo ≤ `cold_days` | Mucho esfuerzo, cero resultado. |
| `frio` | 0 ventas y última actividad > `cold_days` | Sin actividad reciente. |
| `nuevo` | Primera actividad ≤ 7 días y aún sin señales | Recién llegado. |

`poco_rentable` **no** es “riesgo”: es una etiqueta comercial para priorizar. Un curioso legítimo no se bloquea.

### 5.5 Señales de riesgo/abuso

| Señal | Severidad | Peso | Detección | Evidencia |
|---|---|---|---|---|
| `abuso_verbal` | critical | 40 | Regex conservador de insultos/amenazas en inbound | IDs de mensaje |
| `pedido_falso` | critical | 40 | ≥2 ventas canceladas o `NEEDS_OPERATOR` por teléfono/dirección inválidos | IDs de venta |
| `flood` | warning | 25 | ≥3 eventos de rate limit del cliente en 1 h | Eventos |
| `scraping_catalogo` | warning | 20 | ≥15 consultas de producto en 30 min sin venta ni datos de entrega | Turnos |
| `cancelaciones_repetidas` | warning | 20 | ≥2 ventas canceladas | IDs de venta |
| `reclamos_repetidos` | warning | 15 | ≥3 turnos con intent `queja` | Turnos |
| `muchas_preguntas_sin_compra` | info | 10 | ≥20 turnos y 0 ventas liquidadas, en ≥2 conversaciones | Turnos |
| `posible_duplicado` | info | 5 | Teléfono ya asociado a otro cliente | Contactos |

**Reglas de seguridad:**

- Solo las señales **CONFIRMED** suman al `risk_score`. Las `OPEN` se muestran como “por revisar” y no cambian el segmento.
- El operador confirma o descarta con un clic; ambas acciones quedan en `events`.
- **Nunca se bloquea automáticamente.** Bloquear es una acción manual y reversible.
- El cliente bloqueado **no recibe ningún mensaje** (ni siquiera “estás bloqueado”). En `ingest.ingest_incoming`, tras resolver el cliente: se guarda el mensaje para auditoría, **no** se crea turno ni job, y se registra `blocked_inbound`.
- Las señales se detectan en un job del scheduler cada 5 minutos (y con un botón “Escanear ahora”). **No** se ejecutan dentro del turno: la prioridad es la latencia.

### 5.6 API

Router nuevo `api/routes_customers.py`.

| Método | Ruta | Uso |
|---|---|---|
| `GET` | `/api/customers` | Listado paginado con filtros. |
| `GET` | `/api/customers/summary` | KPIs: total, nuevos 7 d, activos 7 d, compradores, conversión, riesgo, bloqueados. |
| `GET` | `/api/customers/{id}` | Perfil + métricas + contactos + zonas + etiquetas + notas + señales + conversaciones + ventas. |
| `PATCH` | `/api/customers/{id}` | Nombre, estado, zona habitual, notas. |
| `GET` | `/api/customers/{id}/contact?reveal=true` | Teléfono/dirección completos; **auditado**. |
| `POST` | `/api/customers/{id}/tags` | Añadir etiqueta. |
| `DELETE` | `/api/customers/{id}/tags/{tag}` | Quitar etiqueta. |
| `POST` | `/api/customers/{id}/notes` | Añadir nota. |
| `POST` | `/api/customers/{id}/signals/{signal_id}/confirm` | Confirmar señal. |
| `POST` | `/api/customers/{id}/signals/{signal_id}/dismiss` | Descartar señal. |
| `POST` | `/api/customers/{id}/block` / `unblock` | Bloquear/desbloquear. |
| `POST` | `/api/customers/merge` | Fusionar dos clientes (manual, con vista previa). |
| `POST` | `/api/customers/scan-signals` | Forzar el escaneo (operador). |

**Filtros del listado:** `q` (nombre, teléfono normalizado, handle, orden), `segment`, `risk`, `status`, `kind` (por defecto `customer`), `zone_id`, `tag`, `has_sales`, `from`, `to`, `sort`, `limit`, `offset`.

**Enmascarado:** el listado y el detalle devuelven `phone_masked` (`+53 5***-***45`) y `address_masked`. El endpoint `contact?reveal=true` devuelve los valores completos y registra `customer_pii_revealed` con el campo revelado.

**Merge:** `POST /api/customers/merge` con `{from_id, into_id, reason}`. En una transacción mueve conversaciones, mensajes (por conversación), ventas, hechos, contactos, etiquetas, notas y señales; deja `from.merged_into_id = into.id`, `from.status = 'MERGED'` y escribe `customer_merges`. La vista previa (`GET /api/customers/merge/preview?from_id=&into_id=`) muestra qué se movería antes de confirmar. Es irreversible desde el panel: se avisa.

### 5.7 UI

- Ruta `/customers`, icono `Contact` (Grupos ya usa `Users`).
- `customers_panel.tsx` (KPIs + filtros + tabla).
- `customer_detail.tsx` (perfil, métricas, historial, señales, notas).
- `customer_signals.tsx` y `customer_merge_dialog.tsx`.

**KPIs:** total de clientes, nuevos (7 d), activos (7 d), compradores, conversión, comisión cobrada (USD/CUP), clientes en riesgo, bloqueados.

**Tabla:** Cliente (nombre + handle), Teléfono (enmascarado), Canales, Zona, Conversaciones, Preguntas, Ventas, Ingresos/Comisión, Conversión, Última actividad, Segmento, Riesgo, Etiquetas.

**Detalle:**

- Cabecera: nombre, segmento, riesgo, estado, botones (etiqueta, nota, bloquear, fusionar).
- Perfil: contactos enmascarados + “Revelar”, zonas y direcciones usadas, primera/última actividad.
- Métricas: tarjetas de esfuerzo y resultado, con la definición en tooltip.
- Historial: conversaciones (enlace al panel A) y ventas (enlace a Finanzas).
- Señales: lista con evidencia, botones Confirmar/Descartar.
- Notas y etiquetas: alta/baja.

**Fusionar:** buscar duplicado, previsualizar conteos, confirmar con diálogo de advertencia y redirigir al cliente destino.

### 5.8 Privacidad y seguridad

- **Enmascarado por defecto** y revelado auditado (`events`).
- **PIN:** hoy el valor por defecto es `0000` y no hay límite de intentos. Con PII en el panel se propone: cambiar el default, añadir rate limit al login y documentar que el dashboard debe ir en `127.0.0.1` salvo que se use LAN confiable.
- **Retención:** **6 meses por defecto**, configurable en `settings` (`customers.message_retention_days`) y editable desde el dashboard sin tocar código. Perfil, ventas y comisiones se conservan por auditoría. El borrado de un cliente **anonimiza** (nombre, teléfono, dirección, contactos) y conserva las ventas con su `customer_id` para no romper finanzas; se registra el evento. La exposición de esta y las demás políticas queda anotada para el **panel de Configuración** (§12).
- **Trazas y logs:** siguen enmascarando PII (`ai/redact.py`); se añaden tests para que un teléfono nuevo no se filtre.
- **Exportación CSV:** opcional, con PIN, explícita y enmascarada por defecto. No se incluye en v1 salvo que se pida.

### 5.9 Criterios de aceptación

- Buscar por teléfono devuelve el cliente sin mostrarlo completo.
- Un cliente con una venta `SETTLED` aparece como `comprador`; con 3+, como `vip`.
- Un cliente con 25 turnos y 0 ventas aparece como `poco_rentable`, no como `riesgo`.
- Una señal `OPEN` no altera el segmento; al confirmarla, sí.
- Bloquear a un cliente hace que su siguiente mensaje se guarde pero **no** genere turno, job ni llamada al LLM (test).
- Fusionar mueve conversaciones y ventas, y el id viejo redirige al nuevo (test).
- El listado responde en <300 ms p95 con 1.000 clientes de prueba.

---

## 6. Integraciones entre paneles

- **Conversaciones → Clientes:** botón “Ver cliente” en la cabecera del chat.
- **Clientes → Conversaciones:** historial con enlaces a cada chat.
- **Clientes → Finanzas:** ventas con enlace a la venta y a la comisión.
- **Conversaciones → Trazas:** últimas trazas del turno, enlace al panel existente.
- **General:** añadir KPIs “clientes activos”, “conversión” y “clientes en riesgo”.
- **Logs:** nuevos eventos `customer_blocked`, `customer_unblocked`, `customer_pii_revealed`, `customer_merged`, `customer_signal_confirmed`, `customer_signal_dismissed`, `blocked_inbound`.

---

## 7. Fases y entregables

| Fase | Contenido | Pruebas | Estimación |
|---|---|---|---|
| **F0** | Migración 020, `turns.record_outcome`, backfill, tests | Turno con/sin venta, turno fallido, backfill idempotente | 0,5–1 d |
| **F1** | API de conversaciones + panel listado/detalle + enmascarado | Filtros, paginación, detalle, cierre, PII | 1–1,5 d |
| **F2** | Migración 021, captura de identidad/contactos, backfill desde ventas y hechos | Normalización, duplicados, no auto-merge | 1 d |
| **F3** | API de clientes + panel + etiquetas/notas/señales/bloqueo/merge | Métricas, segmentos, bloqueo, merge | 1,5–2 d |
| **F4** | Job de señales, settings (incl. retención de mensajes a 6 meses), KPIs, documentación y prueba con datos reales | Señales, umbrales, retención, rendimiento, docs | 1 d |

Cada fase se puede commitear y probar por separado. **No se hace push sin pedido explícito.**

**Orden recomendado:** F0 → F1 (panel de Conversaciones usable) → F2 → F3 → F4. El panel de Conversaciones no depende de 021 salvo `kind`, que ya llega en 020.

---

## 8. Backfill de datos existentes

Script idempotente `backend/scripts/backfill_customers.py` (con `--dry-run`):

1. `customers.kind='supplier'` para identificadores que coincidan con `suppliers.telegram_id` o `telegram_peer_id`.
2. `customers.first_seen_at` = mínimo de `conversations.created_at`; `last_seen_at` = máximo de `messages.created_at`.
3. Teléfono/zona/dirección: primero `preferences_json`, luego la última venta no cancelada, luego el último `conversation_facts`. Solo rellena si el campo está vacío.
4. `customer_contacts`: canal de cada `customer_channels` y teléfonos de ventas/hechos.
5. `turns` desde `traces` (script separado de F0).
6. Reporte final: clientes actualizados, contactos creados, duplicados detectados, turnos sin datos.

Nada se borra. Los duplicados detectados se dejan como señal, no se fusionan.

---

## 9. Riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| Exposición de PII en el dashboard | Enmascarado por defecto, revelado auditado, PIN con rate limit, bind local por defecto. |
| Falsos positivos de “riesgo” | Solo señales confirmadas suman; evidencia visible; nunca bloqueo automático. |
| Bloquear a un cliente real por error | Acción manual, reversible, con evento y confirmación. |
| El panel de conversaciones se vuelve operación manual | v1 sin envío de mensajes; si se pide takeover, se diseña aparte con el gobernador de ritmo. |
| Métricas caras o lentas | Índices, paginación y agregados; cache `customer_metrics` solo si p95 > 200 ms. |
| Fusionar dos personas distintas | Vista previa + confirmación + auditoría; no automático. |
| Mezclar USD y CUP | Métricas separadas por moneda; nunca se suman. |
| Conversaciones de TCP contaminan métricas | `customers.kind` y filtro por defecto `customer`. |
| Deriva de esquema | Migraciones aditivas, tests de migración y backfill con dry-run. |

---

## 10. Decisiones cerradas (2026-11-17)

| Tema | Decisión del operador |
|---|---|
| **Acciones en Conversaciones v1** | Lectura + acciones administrativas: cerrar, etiquetar y bloquear. **Sin envío manual de mensajes.** |
| **PII** | Enmascarada por defecto; “Revelar” muestra el dato completo y queda auditado. |
| **Bloqueo** | El bot deja de responder por completo: guarda el mensaje para auditoría, no crea turno, ni job, ni llamada al LLM. No se avisa al bloqueado. |
| **Rentabilidad** | Esfuerzo vs resultado. Sin costo monetario inventado; queda como mejora opcional futura. |
| **Retención** | **6 meses por defecto**, configurable desde el dashboard (`customers.message_retention_days`). Perfil, ventas y comisiones permanentes. Anotado para el panel de Configuración (§12). |
| **Duplicados** | Fusión manual con vista previa y confirmación. Nunca automática por teléfono. |

---

## 11. Fuera de alcance (explícito)

- Enviar mensajes manualmente desde el dashboard.
- Automatizar bloqueos o respuestas punitivas.
- Scoring con LLM: las señales son reglas deterministas y auditables.
- WhatsApp real (el modelo es canal-agnóstico, pero el adaptador no existe aún).
- Exportación masiva de PII.

---

## 12. Panel de Configuración (futuro, anotado)

El operador pidió exponer como configuración la retención de mensajes y, en general, las políticas que hoy viven en `settings` o en código. No entra en las 5 fases de este plan, pero queda anotado para no dispersar valores por el código.

**Qué expondría**

| Grupo | Claves | Hoy |
|---|---|---|
| Privacidad | `customers.message_retention_days` (6), `llm.trace_retention_days` (30), `llm.trace_prompts` | `settings` / migración 020 |
| Segmentos | `customers.segment.*` (VIP, potencial, frío, poco rentable) | migración 021 |
| Riesgo | `customers.risk.medium` (30), `customers.risk.high` (60) y pesos de señales | migración 021 |
| Ventas | modo de venta, follow-up, política de transporte | ya en Estrategia |
| Catálogo | auto-aplicar precio/stock/specs, delta máximo, confianza mínima | ya en Catálogo |
| Seguridad | PIN, rate limit del login, bind local/LAN | `config` (env), requiere reinicio |

**Diseño mínimo**

- `GET /api/settings/panel` devuelve los grupos con valor actual, tipo, mínimo/máximo y descripción.
- `PUT /api/settings/panel/{group}` valida y escribe en `settings`; los cambios de seguridad que viven en `config` se muestran como solo lectura con la instrucción de `.env`.
- Cada cambio queda en `events` (`setting_changed`) con valor anterior y nuevo.
- UI: ruta `/settings`, formulario por grupos, guardar por grupo, sin reiniciar para las claves de `settings`.

**Orden sugerido:** después de F4, cuando ya existan los umbrales de segmentos y riesgo que este panel va a editar. Mientras tanto, F4 solo añade la clave de retención y un endpoint puntual si hace falta.
