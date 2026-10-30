---
tipo: modelo_datos
proyecto: Cuadra
estado: implementado (migraciones 001–004)
actualizado: 2026-10-21
tags: [sqlite, schema, ventas, comisiones]
---

# 02 — Modelo de Datos (SQLite)

> Principio rector: **el producto es una presentación abstracta del TCP**. No existe catálogo canónico; dos TCPs ofreciendo "lo mismo" son dos filas distintas que el agente puede comparar en tiempo de respuesta.
>
> **Idioma:** el esquema (tablas, columnas, valores) está **en inglés** — es el contrato de código. La documentación (este archivo) permanece en español. Fuente ejecutable: `backend/migrations/001_init.sql`.

## 1. Entidades y relaciones (visión)

```
suppliers 1───N products 1───N product_images
categories 1───N products
suppliers 1───N sales N───1 customers
customers 1───N customer_channels
customers 1───N conversations 1───N messages
conversations 1───N escalations
conversations 1───N sales            (la venta nace de una conversación)
broadcast_groups N───N categories    (group_categories)
broadcast_groups 1───N broadcast_posts   (log de lo publicado)
broadcasts 1───N broadcast_posts
broadcasts N───1 products (opcional)
jobs                                   (cola persistente, sin FK)
events                                 (auditoría, sin FK)
```

## 2. Esquema SQL (migración `001_init.sql`)

Verbatim en `backend/migrations/001_init.sql`. Resumen de referencia:

| Tabla | Rol | Estados / valores |
|---|---|---|
| `categories` | Taxonomía operativa (curaduría del operador) | `active` 0/1 |
| `suppliers` | Proveedores (TCP) | `active` 0/1 |
| `products` | Presentación de UN proveedor | `status`: `ACTIVE`, `OUT_OF_STOCK`, `PAUSED`, `REMOVED`; `currency`/`commission_currency`: `USD`, `CUP` |
| `product_images` | Galería por producto (ruta relativa bajo `data/fotos/product_{id}/`) | `is_main`, `sort_order` |
| `customers` | Cliente con identidad multi-canal | — |
| `customer_channels` | `(channel, identifier)` único por cliente | `channel`: `telegram`, `whatsapp` |
| `conversations` | Sesión de atención | `status`: `ACTIVE`, `WAITING_TCP`, `CLOSED`; `identity`: `bot`/`userbot` (cuenta por la que entró y se responde) |
| `messages` | Historial completo (contexto del LLM y auditoría) | `direction`: `INBOUND`/`OUTBOUND`; `author`: `customer`/`bot`/`tcp`/`operator`; `type`: `text`/`audio`/`image`/`document` |
| `escalations` | Bucle crítico de consulta al TCP | `status`: `PENDING`, `ANSWERED`, `RESOLVED`, `CANCELLED`, `TIMEOUT` |
| `sales` | Ventas y comisiones | `status`: `DRAFT` → `PENDING_APPROVAL` → `CONFIRMED` → `PAID` \| `CANCELLED`; `source`: `bot`/`operator`; `commission_type`: `FIXED`/`PERCENT` |
| `broadcast_groups` | Grupos de difusión (canal-agnósticos) | `status`: `ACTIVE`, `PAUSED`, `BLOCKED`; `sender`: `bot`/`userbot` (default `userbot`); topes `max_posts_per_day`, `min/max_interval_s` (segundos); `peer_id` cachea el id de Telethon ya resuelto |
| `group_categories` | Categorías permitidas por grupo (M:N) | — |
| `zones` | Catálogo de zonas de mensajería | `name` único, `active` |
| `group_zones` | Zonas asignadas por grupo (M:N) | — |
| `supplier_delivery_zones` | Zonas de entrega asignadas a cada TCP (M:N) | — |
| `broadcasts` | Campañas (plantilla con spintax) | `status`: `SCHEDULED`, `SENDING`, `FAILED`, `CANCELLED` (recurrentes: `SCHEDULED` se reprograma cada ciclo; `COMPLETED` quedó obsoleto) |
| `broadcast_group_links` | Selección manual opcional de grupos | — |
| `broadcast_posts` | Log auditable de cada publicación (una fila por intento) | `status`: `QUEUED`/`SENT`/`FAILED`; `image_path`, `attempts`, `error`, `window_start` |
| `jobs` | Cola persistente (sobrevive apagones) | `status`: `PENDING`, `IN_PROGRESS`, `COMPLETED`, `FAILED`; `rate_limit_hits` cuenta aparte los `FloodWait` |
| `sender_state` | Cooldown global por cuenta de envío y último envío | `sender` (`userbot`), `blocked_until`, `last_action_at`, `reason` |
| `events` | Auditoría del sistema | `source`: `system`/`bot`/`dashboard`/`scheduler`/`llm`/`stt` |
| `settings` | Configuración mutable por clave | Defaults de difusión en `broadcast.default_*`; recurrencia en `broadcast.repeat_*` |
| `schema_migrations` | Runner de migraciones (creada por `db.py`) | — |

> **El esquema ejecutable manda:** la fuente de verdad son las migraciones de `backend/migrations/` aplicadas en orden (`001` base, `002` settings, `003` sender/imágenes, `004` userbot/zona/estrategia, `005` identidad de conversación, `006` ciclos de difusión, `007` zonas, `008` zonas por TCP, `009` rate limits de Telegram). Este documento es el resumen de referencia; si divergen, se corrige el documento.

## 3. Notas de implementación

- **WAL + un solo escritor:** todo `INSERT/UPDATE` sale de cuadra-core. El dashboard escribe únicamente vía la API REST del core (nunca abre la BD directamente).
- **`products.specifications_json`:** extensiones JSON1 de SQLite disponibles por defecto. Ejemplo:
  `{"car_cable": "Sí, incluido", "warranty": "6 meses", "beach_delivery": "24h"}` — se enriquece con cada respuesta del TCP y se inyecta en el contexto del LLM para no volver a preguntar.
- **Fotografía histórica en `sales.context_json`:** al crear la venta se congela precio, moneda, comisión y zona del TCP del momento — una auditoría no puede depender de datos editables del catálogo.
- **Moneda explícita en todo:** ni precios ni comisiones sin `currency`/`commission_currency`. **`commission_currency` es intencional:** lo habitual es cobrar la comisión en USD aunque el precio esté en CUP. Conversiones (si algún día se necesitan) se hacen al registrar la venta, no al consultar.
- **Identidad de cliente:** la llave natural es `(channel, identifier)`. El mismo cliente en Telegram y WhatsApp futuro son dos filas de `customer_channels` apuntando al mismo `customers.id` (el merge es manual/operativo).
- **Rutas de imágenes:** relativas bajo `data/` (p. ej. `fotos/product_12/img_001.jpg`), nunca rutas absolutas — la BD es portable entre máquinas. Las imágenes se comprimen al subir (máx. 1280 px, ~500 KB).
- **Borrado lógico preferido:** `suppliers.active = 0` y `products.status = 'REMOVED'` en lugar de `DELETE` físico (las ventas y el historial referencian estas filas). `DELETE` físico reservado para datos sin referencias (imágenes sueltas, categorías sin usar).
- **Regla de seguridad de categorías (anti-baneo):** el scheduler solo publica un producto en grupos `ACTIVE` cuyas categorías (`group_categories`) incluyan su `category_id`. **Producto con `category_id = NULL` solo va a grupos sin categorías asignadas** (grupos libres). La selección manual (`broadcast_group_links`) tiene prioridad sobre el auto-match, pero la regla de NULL se aplica igual.
- **Userbot (Telethon):** `sender = 'userbot'` (default) publica con una cuenta normal (`backend/data/telegram_user.session`, creada con `scripts/telegram_login.py`). Esa cuenta también **atiende los 1:1 que entran por ella**: `conversations.identity` guarda `bot`/`userbot` y la respuesta sale por la misma cuenta. `sender = 'bot'` queda inactivo para difusiones (el bot sigue atendiendo los 1:1 que entran por él). Si la sesión no está configurada, el grupo falla con un error permanente visible en el log.
- **Solo grupos/canales:** las comunidades no son destinos publicables ni se modelan. `kind` no se implementó; `channel` sigue siendo el transporte (`telegram`/`whatsapp`).
- **Zonas:** `zones` es un catálogo (como `categories`) con CRUD desde el dashboard (Catálogo → Zonas) y relación M:N con los grupos (`group_zones`). El producto hereda `suppliers.delivery_zone` (texto libre del TCP) al renderizar el token `{zona}`; el cruce de cobertura fino y el scoring multi-zona quedan para Fase 2.
- **ID de producto:** las difusiones agregan `ID: <products.id>` si la plantilla no contiene `{id}`. El pipeline resuelve IDs explícitos antes de nombre/imagen/NLU.
- **Estrategia configurable:** `settings` guarda `broadcast.default_max_posts_per_day`, `broadcast.default_min_interval_s`, `broadcast.default_max_interval_s`, `broadcast.repeat_passes` y `broadcast.repeat_window_days`. El panel Estrategia los edita y puede aplicar los topes por grupo a todos los grupos. `repeat_passes` = veces que cada campaña se publica en cada grupo dentro del ciclo; `repeat_window_days` = duración del ciclo (al cerrarse, se reinicia el conteo).
- **Rate limits sin bloquear la cola:** `FloodWait`/`SlowModeWait` (Telethon) y `RetryAfter` (Bot API) se convierten en fallo transitorio para que el worker aplique backoff; nunca se duerme dentro del handler (la cola es única y atendería tarde a los clientes).
- **Vista segura al LLM:** el JSON de producto que se inyecta en el contexto (`catalog.llm_product_json`) **excluye campos internos** (`commission`, `commission_currency`): lo que entra al prompt puede repetirse al cliente. Ver `01_arquitectura.md` §6.1.
- **Límites y recurrencia:** el scheduler consulta `broadcast_posts` por grupo; respeta `max_posts_per_day` (tope de seguridad del día, America/Havana) y el jitter `[min_interval_s, max_interval_s]` en segundos. Cada campaña se republica `repeat_passes` veces por grupo dentro de un ciclo de `repeat_window_days` días (ventana anclada a America/Havana); al completar el ciclo la campaña queda `SCHEDULED` hasta el inicio del siguiente. `status = 'BLOCKED'` es la lista negra explícita.

## 4. Caminos de evolución (documentados, NO implementados — ordenados por fase)

| Fase | Necesidad futura | Cambio |
|---|---|---|
| 2 | Multi-instancia de canal (varios números WA) | Columna `channel_instance` en `conversations` |
| 3+ | Liquidación de comisiones por período | Tabla `commission_settlements(supplier_id, period, total_amount, paid)` |
| 3+ | Operadores del dashboard | Tabla `users` + login real (hoy: PIN compartido) |
| 3+ | Preferencias avanzadas de cliente | Ampliar `customers.preferences_json` (criterios de ranking: precio, zona, disponibilidad) |
| 3+ | Historial de precios del producto | Tabla `price_history(product_id, price, currency, created_at)` |
| 3+ | Reglas avanzadas de publicación por grupo | Columnas en `broadcast_groups`: `max_price`, `window_start/end`, `allowed_days` |
| 3+ | Difusión multi-producto (catálogo semanal) | Tabla `broadcast_products(broadcast_id, product_id)` |
| Si se necesita | Deduplicar "el mismo producto" en TCPs | Agregar `base_products` + `offers(base_product_id, supplier_id, …)`; los productos existentes pasan a ser ofertas |
