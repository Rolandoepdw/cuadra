---
tipo: revision_tecnica
proyecto: Cuadra
modulo: viabilidad_agente_autonomo
estado: DISEÑO APROBADO — listo para implementar
revisado: 2026-11-03
aprobado: 2026-11-03
tags: [viabilidad, auditoria, discordancias, decisiones, implementacion]
---

# 05 — Viabilidad de la propuesta y cambios requeridos

> **Estado:** el dueño aprobó D1–D10 el **2026-11-03**. Este documento ya no es una
> solicitud de decisión: es el registro de la revisión y el plan de implementación.
>
> **Regla de lectura:** el código y el esquema real mandan sobre la documentación.
> Lo que aparece en la columna **“Corrección aplicada”** ya fue ajustado en los
> documentos `00`–`04`; las decisiones están en la sección 4 y las fases en la sección 5.

---

## 1. Veredicto en una página

**La propuesta es técnicamente viable en el stack actual** (Windows, SQLite, un solo
proceso Python, 4 GB de VRAM, $0), pero **no era implementable tal como estaba escrita**.
Los problemas no son de concepto: son de contrato con el código real, de estados de
base de datos, de idempotencia y de una autonomía que choca con decisiones ya cerradas.

| Pregunta | Respuesta |
|---|---|
| ¿Se puede implementar sin Docker/Postgres/SaaS? | **Sí.** Todo cabe en SQLite + FastAPI + llama.cpp + CPU. |
| ¿Se puede implementar tal como estaba en `00`–`04`? | **No.** Había columnas, tablas y estados que no existen; migraciones duplicadas; y un modelo que no está instalado. |
| ¿El agente puede cerrar ventas y actualizar catálogo desde el TCP? | **Sí, con guardrails.** Sin ellos, un error de un modelo 3B puede vender a precio equivocado o pisar stock reservado. |
| ¿La autonomía total es obligatoria? | **No.** Se puede empezar en modo asistido (operador aprueba con un clic) y activar el modo autónomo cuando el sistema demuestre precisión. |
| ¿Cuánto trabajo es? | **Orden de magnitud: 6–9 semanas de desarrollo enfocado**, en 6 fases (0–5). No incluye pruebas de campo ni carga de datos reales. |
| ¿Qué es lo primero? | Medir. Hoy no hay `llm_calls`, ni golden set, ni trazas de prompt/completion. Sin eso, cualquier cambio es a ciegas. |

**Recomendación:** aprobar la arquitectura corregida y empezar en modo `assisted` con
precio auto-aplicado dentro de límites. Al cumplir las métricas de aceptación se cambia
`sales.mode` a `autonomous`, sin reescribir código.

---

## 2. Discordancias encontradas (documento vs. código real)

Severidad: **BLOQUEANTE** = la implementación falla o corrompe datos; **ALTA** = funciona
mal o genera deuda peligrosa; **MEDIA** = fricción o inconsistencia.

| # | Sev. | Lo que decía la propuesta | Lo que hay realmente | Corrección aplicada |
|---|---|---|---|---|
| 1 | BLOQUEANTE | `Qwen2.5-3B-Instruct-Q5_K_M.gguf` “en ejecución” | `backend/models/` solo tiene `Bonsai-8B-Q1_0.gguf` (default actual) y `NVIDIA-Nemotron-3-Nano-4B-Q4_K_M.gguf`. Qwen no está descargado. | El modelo pasa a ser **candidato a bake-off**, no una verdad asumida. Fase 0 lo mide. |
| 2 | BLOQUEANTE | Estados de venta `ORDER_PLACED`, `DISPATCH_NOTIFIED`, `DELIVERED_PENDING_SETTLEMENT`, `SETTLED` | `sales.status` tiene `CHECK (DRAFT, PENDING_APPROVAL, CONFIRMED, PAID, CANCELLED)`. Insertar otro estado falla. | Se rediseña la tabla `sales` en la migración `014` con estados nuevos y migración de `PAID`→`SETTLED`. |
| 3 | BLOQUEANTE | `ALTER TABLE sales ADD COLUMN order_code TEXT UNIQUE` | SQLite **no permite** añadir una columna `UNIQUE` con `ALTER TABLE`. Falla. | Se añade la columna y luego un `CREATE UNIQUE INDEX`. |
| 4 | BLOQUEANTE | `INSERT INTO sales (... source ...) VALUES (..., 'telegram', ...)` y sin `supplier_id` | `sales.supplier_id` es `NOT NULL`; `source` solo acepta `'bot'` o `'operator'`. Falla. | El insert incluye `supplier_id`, `conversation_id` y `source='bot'`. |
| 5 | BLOQUEANTE | `commission_ledger` definido en `012` y otra vez distinto en `014`; migraciones `012/013/014` con contenidos cruzados | La última migración real es `011`. No existe `012`. | Se consolida en una sola secuencia: `012_observability_and_turns`, `013_memory_retrieval`, `014_sales_and_finance`, `015_catalog_mutations`. |
| 6 | BLOQUEANTE | FTS5 indexa `products.brand` y `products.category` | `products` **no tiene** `brand` ni `category`; solo `category_id` → `categories.name`. | FTS5 indexa `name`, `description`, `specifications_json` y el nombre de categoría por `JOIN`. |
| 7 | BLOQUEANTE | `SELECT ... delivery_zone FROM products` | `products` no tiene `delivery_zone`. La cobertura real está en `supplier_delivery_zones` (N:M con `zones`) y queda un `suppliers.delivery_zone` legado. | `llm_product_json` devuelve `zonas_entrega` como **lista** desde `supplier_delivery_zones`. |
| 8 | BLOQUEANTE | `reload_vectors()` lee de `product_embeddings` | Esa tabla **no existe** en ninguna migración. | Se crea `product_embeddings` en `013` y se define el job de reindexado. |
| 9 | BLOQUEANTE | `SELECT summary, summary_through FROM conversations` | Esas columnas no existen; el resumen vive en `conversations.context_json.summary` / `summary_through`. | El código de memoria usa `conversations.get_context/set_context`. |
| 10 | ALTA | `WHERE id > summary_through` | `summary_through` es un **conteo**, no un id de mensaje. Con ids con huecos se resume mal. | Se guarda `summary_through_message_id` (último id resumido). |
| 11 | ALTA | `Depends(get_db)` en FastAPI | No existe `get_db`; el patrón real es `db.connect()` dentro de un helper `_run` y `Depends(require_pin)` con header `X-PIN`. | Todos los ejemplos usan el patrón real. |
| 12 | ALTA | `INSERT INTO events (event_type, reference_id, payload_json)` | `events` tiene `source, type, category, entity, entity_id, details_json` y se escribe con `events.log(...)`. | Se usa `events.log` en todos los ejemplos. |
| 13 | ALTA | `cursor.execute("BEGIN TRANSACTION")` dentro de servicios | `sqlite3` con `isolation_level` por defecto ya abre transacción; un `BEGIN` explícito puede lanzar “cannot start a transaction within a transaction”. | Se usa `with conn:` y se evita el `BEGIN` manual. |
| 14 | ALTA | `InboundMessage.channel_message_id` / deduplicación | `messages` no guarda id externo ni `update_id`. Un reinicio de polling puede reprocesar. | Se añade `messages.external_id` + índice único por conversación. |
| 15 | BLOQUEANTE | “El pipeline procesa audio del TCP con Whisper” | `_ingest_supplier` encola `tcp_reply` incluso para audio; `tcp_reply` lee `message['body']`, que es `None` en audio. **Hoy el audio del TCP no se transcribe.** | Se separa `transcribe_audio` (con `next_job`) y `tcp_process`; el audio TCP se transcribe antes de clasificar. |
| 16 | BLOQUEANTE | Diccionario hardcodeado de “16 municipios” de La Habana | `zones` es un CRUD; las 10 filas actuales son datos de prueba y pueden ser cualquier zona. No hay una lista fija de municipios. | `zones` (activas) es la fuente de verdad; `zone_aliases` mapea nombres coloquiales; las zonas se desactivan, no se borran. |
| 17 | ALTA | Teléfono cubano “8 dígitos empezando en 5 o 6” | Los fijos de La Habana empiezan en 7; esa regla rechaza direcciones válidas. | Se aceptan 6–8 dígitos, con `+53` opcional, y se normaliza. |
| 18 | ALTA | Extractor permite `MLC` | `products.currency` y `sales.currency` tienen `CHECK (USD, CUP)`. `MLC` no se puede guardar. | `MLC` va a revisión; el operador decide si se mapea a `USD`. |
| 19 | ALTA | Comisión sin moneda | `products.commission_currency` existe; `sales` no la tiene. Mezclar USD y CUP rompe el cálculo de `supplier_payout`. | Se añade `commission_currency` a `sales` y `commission_ledger`; no se suma USD+CUP en métricas. |
| 20 | ALTA | Descontar `products.stock` al crear la venta y luego el TCP manda stock absoluto | Una actualización absoluta del TCP pisa las reservas de ventas pendientes. | Se añade `products.stock_reserved`: reservar al vender, liberar al cancelar, descontar físico al entregar. |
| 21 | ALTA | Undo “restaura el estado exacto anterior” sin comprobar cambios posteriores | Si hay dos mutaciones, revertir la primera pisa la segunda. | El revert compara `updated_at`/estado actual con `new_state_json`; si cambió, responde `409`. |
| 22 | MEDIA | `order_code = V-{random 4 dígitos}` | Colisiones posibles; el `UNIQUE` fallaría al azar. | `order_code = V-{id:06d}` generado después del insert. |
| 23 | ALTA | `TurnAssembler` en memoria con `asyncio.TimerHandle` | Un apagón pierde los mensajes bufferizados (Cuba: apagones frecuentes). Además se indexa por `customer_id`, no por conversación. | Se reemplaza por `turns` persistente en SQLite + job `process_turn` con `due_at`. |
| 24 | MEDIA | Test de ráfaga: “exactamente 1 llamada al LLM” | El pipeline hace NLU + (a veces) composición: son 1–2 llamadas por turno. | El criterio es **1 turno → 1 respuesta**, no 1 llamada. |
| 25 | MEDIA | `pensamiento_interno` + el LLM redacta la confirmación de venta | Gasta tokens, expone razonamiento y puede inventar precio/dirección en un mensaje crítico. | Se elimina el CoT; el backend renderiza cotización, confirmación y orden con plantillas. |
| 26 | MEDIA | `response_format: json_object` | El cliente actual ya usa `json_schema` con `strict: true` y `--jinja`. | Se mantiene `json_schema`; `json_object` solo como fallback. |
| 27 | MEDIA | `jinja2`, `fastembed`, `pytest` como si estuvieran instalados | No están en `requirements.txt`; `fastembed` y `pytest` no están en el venv. `numpy` y `onnxruntime` sí. | Se añaden dependencias o se usa `prompts.py` + `unittest` (decisión). |
| 28 | ALTA | Trazas de prompt/completion “recomendadas” | Solo existen metadatos en `events`; no hay `llm_calls`. | Se añade `llm_calls` en `012` con retención y redacción de PII. |
| 29 | MEDIA | “Notifica al operador” | No hay chat/id de operador ni canal de notificación. | Decisión D6: solo dashboard; no se añade setting de Telegram del operador. |
| 30 | ALTA | Correlación TCP por “la más antigua”; si hay ambigüedad, “la más reciente” | Riesgo de aplicar un precio/stock al producto equivocado. | `[E-###]` por id de escalación; si hay ambigüedad, **no se aplica**: va a `PENDING_REVIEW`. |
| 31 | MEDIA | `items_json` para combos futuros | `sales` es de un solo producto; un JSON no es un modelo relacional. | Se difiere multi-producto a una futura tabla `sale_items`. |
| 32 | MEDIA | `bge-small-en-v1.5` como modelo de embeddings | Es inglés; el catálogo y los clientes son en español. | Se usa un modelo multilingüe (`paraphrase-multilingual-MiniLM-L12-v2`). |
| 33 | ALTA | FastEmbed descarga el modelo al arrancar | Viola el requisito offline y depende de Hugging Face en el primer uso. | El modelo se descarga una vez y se guarda en `backend/models/fastembed/`. |
| 34 | MEDIA | `products_fts` creado sin backfill | Los 32 productos actuales no aparecerían hasta editarlos. | El `013` incluye `INSERT INTO products_fts SELECT ...`. |
| 35 | MEDIA | Búsqueda filtra `stock > 0` | Un cliente que pregunta por un producto agotado recibiría “no existe” en vez de “está agotado”. | Se incluye `OUT_OF_STOCK` y se devuelve el estado real. |
| 36 | MEDIA | `sales.status = PAID` al liquidar comisión | `PAID` ya existe, pero en un comisionista el TCP cobra al cliente y luego paga a Cuadra. Son dos eventos distintos. | Se separan `DELIVERED` (TCP entregó/cobró) y `SETTLED` (Cuadra cobró su comisión). |
| 37 | MEDIA | `commission_ledger.status` duplica estados de `sales` | Dos fuentes de verdad que se desincronizan. | El ledger solo maneja `PENDING/COLLECTED/VOID`; el ciclo de entrega vive en `sales`. |
| 38 | MEDIA | Guardrails “verificadores de precio” sin diseño | No hay validador de salida ni blocklist. | Se define `guardrails.py` con precios permitidos, términos internos, longitud y JSON. |
| 39 | MEDIA | `01_arquitectura.md` menciona panel de Conversaciones | Ese panel no existe en `dashboard/src/panels/`. | Se anota como deuda de documentación; no se asume. |
| 40 | BAJA | Se habla de “1,000 productos” como si fuera el catálogo actual | Hay 32 productos. | Se dice “proyección”; el fuzzy actual se mantiene como fallback. |
| 41 | BLOQUEANTE | `calculate_commission` lee `products.commission_type` | `products` **no tiene** `commission_type`; solo `commission` y `commission_currency`. `sales` sí la tiene. | Se añade `products.commission_type` con default `FIXED` en `014`, y el código usa ese valor. |
| 42 | ALTA | Cierre de conversación con `closed_at` | `conversations` **no tiene** `closed_at`; solo `status` y `updated_at`. | Se añade `conversations.closed_at` en `013`. |

### 2.1 Verificaciones ejecutadas

| Verificación | Resultado |
|---|---|
| Esquema real | Migraciones `001`–`011` revisadas; la última aplicada es `011`. |
| Python / SQLite | Python 3.13.3, SQLite 3.49.1. |
| FTS5 | `CREATE VIRTUAL TABLE ... USING fts5` funciona en el venv. |
| Dependencias | `numpy`, `onnxruntime`, `pydantic` presentes; `fastembed`, `pytest`, `jinja2` ausentes. |
| Modelos | `Bonsai-8B-Q1_0.gguf` (1.08 GB) y `NVIDIA-Nemotron-3-Nano-4B-Q4_K_M.gguf` (2.64 GB). |
| Datos actuales | 4 proveedores, 32 productos, 10 zonas (9 activas), 1 cliente, 1 conversación, 0 ventas. |
| Modelo activo | `settings['llm.model'] = Bonsai-8B-Q1_0.gguf`. |
| Audio TCP | `_ingest_supplier` encola `tcp_reply` incluso para audio; `tcp_reply` lee `body` (None). Bug confirmado por lectura de código. |
| Patrón API | `db.connect()` + `Depends(require_pin)` con header `X-PIN`; no existe `get_db`. |

**No verificado en esta revisión:** VRAM real bajo carga, calidad del modelo con
conversaciones reales, comportamiento de Telegram en producción. Eso es Fase 0.

---

## 3. Lo que ya quedó corregido en `00`–`04`

1. **Estado del documento:** pasó de `DISENO_APROBADO` sin verificar a `PROPUESTA
   REVISADA` y, tras la aprobación del dueño, a `DISEÑO APROBADO`.
2. **Modelo:** ya no se afirma que Qwen está en ejecución. Se hace un bake-off en Fase 0
   con los modelos que instale el dueño (Bonsai, Nemotron, Qwen o Gemma), midiendo VRAM,
   latencia y calidad; ninguno se asume instalado.
3. **Autonomía:** pasó de “autonomía optimista obligatoria” a **autonomía configurable**:
   - `assisted` (default aprobado): el bot crea la venta en `PENDING_APPROVAL`; el
     operador aprueba con un clic y el bot despacha.
   - `autonomous` (opt-in): el bot crea en `PENDING_DISPATCH` y despacha solo, después
     de cumplir las métricas de aceptación.
   - Mutación de catálogo: stock/specs auto con guardrails; **precio auto con límites**
     (±50 %, confianza ≥ 0.85); fuera de rango va a revisión.
4. **Esquema:** `012` observabilidad/turnos, `013` memoria/retrieval, `014` ventas/finanzas
   y `015` mutaciones de catálogo. Sin duplicados.
5. **Estados:** un solo vocabulario en inglés para el código; la UI los traduce al español.
6. **Memoria:** `turns` persistente, `conversation_facts`, resumen incremental por id de
   mensaje, perfil de cliente en `customers.preferences_json`.
7. **Retrieval:** FTS5 sobre columnas reales; `product_embeddings`; modelo multilingüe;
   backfill; fallback al fuzzy actual.
8. **TCP:** audio transcrito antes de clasificar; triage explícito; `[E-###]`; sin
   auto-aplicación en ambigüedad; undo con detección de conflicto.
9. **Ventas:** `supplier_id`, `conversation_id`, `source='bot'`, `order_code` por id,
   `stock_reserved`, `commission_currency`, estados `DELIVERED` vs `SETTLED`.
10. **API/dashboard:** patrón real `db.connect()` + `require_pin`; endpoints de finanzas
    y auditoría; paneles nuevos.

---

## 4. Decisiones aprobadas (2026-11-03)

Estas son las respuestas del dueño. Ya están reflejadas en `00`–`04`; no queda ninguna
decisión pendiente para empezar la Fase 0.

| ID | Decisión | Efecto en el diseño |
|---|---|---|
| D1 | **Asistido ahora; autónomo después de medir.** | `sales.mode='assisted'` por defecto al aprobar D1. **Actualizado por D23 (2026-11-19):** el default pasa a `autonomous`; `assisted` sigue disponible en el panel. |
| D2 | **Auto con límites.** | `catalog.autoapply.price=1`; solo aplica si la correlación es inequívoca, la confianza ≥ 0.85 y el cambio está dentro de ±50 %. Si no, revisión. |
| D3 | **Catálogo CRUD de zonas.** | `zones` activas es la fuente de verdad. Cualquier zona creada por el operador es válida; no se asume La Habana ni una lista fija. Las zonas se desactivan, no se borran. |
| D4 | **Comisión cruzada.** | `commission_currency` se guarda aparte; si no coincide con la moneda de venta, `supplier_payout=NULL` y el dashboard muestra “requiere conversión”. |
| D5 | **Una venta = un producto.** | `sale_items` y combos quedan diferidos. |
| D6 | **Solo dashboard.** | Sin Telegram al operador. Alertas en Finanzas, Auditoría y Logs. |
| D7 | **Trazas redactadas.** | `ai/redact.py` enmascara teléfono/dirección; `redacted=1`; retención de 30 días. |
| D8 | **Bake-off con los modelos que instale el dueño.** | Se comparan Bonsai, Nemotron y los Qwen/Gemma que se descarguen; no se asume ninguno. |
| D9 | **Mantener `prompts.py`.** | Sin Jinja2. |
| D10 | **Mantener `unittest`.** | Sin pytest. |

### D1 — Autonomía

El bot arrancó en `assisted`: crea la venta en `PENDING_APPROVAL`, el operador aprueba
con un clic y el bot despacha. **Desde D23 (2026-11-19)** el default es
`sales.mode='autonomous'`: crea en `PENDING_DISPATCH` y despacha solo. `assisted` e
`hybrid` siguen disponibles en el panel de Finanzas. El cambio es de configuración,
no de código.

### D2 — Precio

Se auto-aplica solo si:

- la mutación viene de una escalación correlacionada sin ambigüedad;
- la confianza del extractor es ≥ 0.85;
- el precio nuevo está dentro de ±50 % del anterior;
- la moneda es USD o CUP.

Cualquier otro caso crea una fila `PENDING_REVIEW` y no toca el producto.

### D3 — Zonas

`zones` es un CRUD operativo: el operador crea, edita y desactiva zonas de entrega.
Las 10 filas actuales son datos de prueba; pueden ser de La Habana o de cualquier otra
zona. El agente valida contra `zones.active=1` y `supplier_delivery_zones`; `zone_aliases`
solo traduce nombres coloquiales. No se hardcodea ninguna lista geográfica.

### D4–D10 — Resto

- **D4:** la comisión puede ser en moneda distinta a la venta; nunca se suman USD y CUP.
- **D5:** una venta es de un producto; los combos se manejan como ventas separadas.
- **D6:** el operador trabaja desde el dashboard; no se configura Telegram.
- **D7:** se guardan prompts/completions redactados, no textos completos con PII.
- **D8:** el dueño provee los modelos; la elección se hace con datos en Fase 0.
- **D9/D10:** `prompts.py` y `unittest` se mantienen.

---

## 5. Plan de implementación recomendado (corregido)

> Cada fase termina con un criterio verificable. No se pasa a la siguiente sin medir.

### Fase 0 — Línea base y medición (1 semana)

- Migración `012`: `llm_calls` y registro de prompt/completion/latencia/modelo en cada llamada.
- Crear golden set de 50–100 turnos anonimizados (JSONL) con resultados esperados.
- Medir el agente actual: intención, precio correcto, fugas, latencia.
- Bake-off de modelos con el mismo set: Bonsai, Nemotron 4B, candidatos Qwen/Gemma.
- **Aceptación:** informe con métricas base y modelo elegido justificado.

### Fase 1 — Turnos, idempotencia y contexto (1–2 semanas)

- Migración `012`: `messages.external_id` + deduplicación.
- Tabla `turns` + `process_turn` + debounce persistente.
- `generate_reply` idempotente (no duplicar respuesta tras un reinicio).
- `ContextBuilder` con presupuesto real y orden por prioridad.
- `guardrails.py` mínimo (precio permitido, términos internos, longitud).
- **Aceptación:** 3 mensajes seguidos → 1 turno → 1 respuesta; reiniciar el proceso
  a mitad de turno no duplica respuestas; 0 fugas de comisión en 20 ataques.

### Fase 2 — Memoria, hechos y retrieval (2 semanas)

- Migración `013`: `conversation_facts` + extracción de slots dentro del contrato del turno.
- Resumen incremental por `summary_through_message_id`.
- `products_fts` + backfill + triggers.
- `product_embeddings` + FastEmbed multilingüe + job de reindexado.
- `zone_aliases` + validación contra `zones` + cobertura por TCP.
- Perfil de cliente en `customers.preferences_json` + cierre por inactividad.
- **Aceptación:** tras 50 mensajes se conservan producto, zona y presupuesto;
  Recall@3 ≥ 95% en 50 consultas; búsqueda < 10 ms con 1,000 productos simulados.

### Fase 3 — Ventas y finanzas (2 semanas)

- Migración `014`: rebuild de `sales`, `commission_ledger`, `stock_reserved` y
  settings de autonomía. (`zone_aliases` se creó en `013`.)
- `domain/sales.py`: crear venta, reservar stock, cancelar, entregar, liquidar.
- Despacho al TCP con `V-######`; el adaptador devuelve el id del mensaje.
- API `/api/finance/*` + panel de Finanzas.
- Modo asistido por defecto; modo autónomo por setting.
- **Aceptación:** venta de punta a punta en modo asistido; stock reservado/liberado;
  ledger correcto; cancelación reversible; métricas separadas por moneda.

### Fase 4 — TCP, mutaciones y undo (2 semanas)

- Triage de mensajes TCP: escalación, ack de orden, entrega, rechazo, actualización,
  pregunta.
- Audio TCP transcrito antes de clasificar.
- `[E-###]` + correlación; ambigüedad → revisión, no auto-aplicación.
- Migración `015`: `catalog_mutation_log` + `domain/catalog_mutations.py` + política de precio.
- Endpoint `/api/catalog/mutations/{id}/revert` + panel de auditoría.
- Reanudar clientes en `WAITING_TCP` cuando llega el dato.
- **Aceptación:** audio de TCP actualiza stock con log y undo; precio fuera de rango
  va a revisión; revert con cambio posterior devuelve 409; 0 correlaciones cruzadas
  en 20 casos.

### Fase 5 — Endurecimiento y evaluación (1 semana)

- Golden set completo, replay, comparación de modelos/prompts.
- Pruebas de inyección, fugas, cancelaciones, apagones simulados.
- Documentación de operación y runbook.
- **Aceptación:** métricas de la sección 8 del brief cumplidas o desviaciones
  documentadas con plan.

---

## 6. Qué se mantiene del código actual

| Se mantiene | Por qué |
|---|---|
| Cola persistente `jobs`, backoff, rate limits | Ya resuelve apagones, FloodWait y prioridades. |
| `send_message` idempotente por evento | Evita doble envío tras un reinicio. |
| Delays humanos y typing | Parte de la persona; no tocar. |
| `catalog.llm_product_json` | Ya excluye comisión y datos internos. |
| `domain/escalations.py` | Base del bucle TCP; se extiende, no se reescribe. |
| Difusiones, scheduler y dashboard | Fuera del foco; funcionan y están probados. |
| `ai/prompts.py` | Fuente de verdad de la persona; se amplía. |
| `unittest` + `backend/tests` | Ya existe una base; no hace falta otro framework. |

## 7. Qué se cambia

| Se cambia | Motivo |
|---|---|
| `ai/ingest.py` | Debe crear `turns` y deduplicar, no encolar `generate_reply` por mensaje. |
| `ai/pipeline.py` | Debe recibir un turno consolidado y un estado explícito, no solo el último mensaje. |
| `ai/llm.py` | Añadir `llm_calls`, `schema_name`, y contratos nuevos; mantener `Intent`. |
| `domain/conversations.py` | `external_id`, `closed_at`, estado en `context_json`, resumen por id. |
| `domain/catalog.py` | `zonas_entrega` como lista, `stock_reserved`, búsqueda híbrida. |
| `channels/telegram.py` y `telegram_user.py` | `send_text` debe devolver el id del mensaje enviado. |
| `api/routes_*` | Nuevas rutas de finanzas y auditoría con el patrón real. |
| `dashboard/src/App.tsx` | Nuevos paneles Finanzas y Auditoría. |

## 8. Qué se elimina o se difiere

| Elemento | Acción |
|---|---|
| `HABANA_ZONES` hardcodeado | Eliminar; usar `zones` + `zone_aliases`. |
| `pensamiento_interno` | Eliminar del contrato. |
| `items_json` | Diferir; usar `sale_items` si algún día hay combos. |
| Auto-aplicación de precio sin límites | Eliminar; pasa a revisión o límites. |
| `TurnAssembler` solo en memoria | Sustituir por `turns` persistente. |
| Correlación TCP “la más antigua” | Sustituir por `[E-###]` + triage. |
| `product_embeddings` inexistente | Crear en migración. |
| Re-resumen de todo el historial | Sustituir por incremental. |
| `preferences_json` sin uso | Usarlo para perfil de cliente. |
| `WAITING_TCP`/`CLOSED` sin uso | Implementarlos con transiciones reales. |

---

## 9. Riesgos principales y mitigación

| Riesgo | Probabilidad | Impacto | Mitigación |
|---|---|---|---|
| El modelo 3B inventa precio/garantía | Alta | Alto | Guardrails + plantillas para mensajes críticos + golden set. |
| El LLM malinterpreta un audio del TCP y cambia precio | Media | Muy alto | Precio a revisión por defecto; límites; undo con conflicto. |
| Se vende sin stock real | Media | Alto | `stock_reserved`; validación antes de crear; ack del TCP. |
| Se pisa una venta con una actualización absoluta de stock | Media | Alto | Reservas separadas del stock físico; alerta si `reserved > stock`. |
| Correlación TCP cruzada | Media | Alto | `[E-###]`; ambigüedad → revisión; nunca “la más reciente”. |
| Apagón pierde ráfagas | Alta | Medio | `turns` persistente + recuperación al arrancar. |
| Fuga de comisión en una respuesta | Media | Muy alto | Blocklist + verificación de precios + pruebas de inyección. |
| Saturación de contexto | Media | Medio | Presupuesto por prioridad; hechos fijados; resumen incremental. |
| FastEmbed no carga offline | Media | Medio | Modelo vendorizado + fallback fuzzy. |
| Cambio de modelo rompe JSON | Media | Medio | `json_schema` + reintento + validación Pydantic + fallback. |
| Deuda de datos (zonas/monedas) | Alta | Medio | Decisiones D3/D4 antes de implementar. |

---

## 10. Criterios de aceptación globales

| Área | Métrica | Objetivo |
|---|---|---|
| Ráfagas | 3 mensajes seguidos → 1 respuesta | ≥ 95% |
| Memoria | Conserva producto/zona/presupuesto tras 50 mensajes | ≥ 95% |
| Intención | Golden set de 50–100 turnos | ≥ 90% |
| Slots | Producto, cantidad, zona, nombre, dirección, teléfono | ≥ 85% F1 |
| Retrieval | Recall@3 en productos relevantes | ≥ 95% |
| Grounding | Precios/stock inventados | 0 |
| Seguridad | Fugas de comisión/margen en 100 ataques | 0 |
| Ventas | Intención de compra → venta o escalación explícita | ≥ 80% |
| TCP | Correlaciones cruzadas | 0 |
| Undo | Revert con cambio posterior | 409, no corrupción |
| Latencia | p95 en local | < 20 s (ajustar a operación) |
| Apagón | Reinicio a mitad de turno | 0 respuestas duplicadas |

---

## 11. Lo que NO se implementó en esta revisión

- No se tocó código de producción.
- No se crearon migraciones.
- No se instalaron dependencias.
- No se descargó ningún modelo.
- No se modificó la base de datos real.
- No se hizo commit.

Este documento y los `00`–`04` corregidos fueron **aprobados el 2026-11-03**. A partir
de aquí son la referencia de implementación, no una propuesta.

> **Actualización posterior:** la Fase 0 ya está implementada en el árbol de trabajo
> (`012_observability_and_turns.sql`, trazas redactadas, retención, golden set y
> `scripts/eval_agent.py`). Esos cambios de código todavía no están commiteados.

---

## 12. Checklist de aprobación (completado)

- [x] D1: modo inicial → asistido, con paso a autónomo al cumplir métricas.
- [x] D2: política de precio → auto con límites.
- [x] D3: zonas → CRUD `zones`; sin lista fija de municipios.
- [x] D4: moneda de comisión → cruzada, con `supplier_payout=NULL` si difiere.
- [x] D5: multi-producto → diferido; una venta = un producto.
- [x] D6: notificación al operador → solo dashboard.
- [x] D7: trazas → redactadas, retención de 30 días.
- [x] D8: modelo → bake-off en Fase 0 con los modelos que instale el dueño.
- [x] D9: prompts → mantener `prompts.py`.
- [x] D10: tests → mantener `unittest`.
- [x] Orden de fases y criterios de aceptación aceptados.
- [x] Autorizado empezar por Fase 0 (medición) sin tocar la operación real.
