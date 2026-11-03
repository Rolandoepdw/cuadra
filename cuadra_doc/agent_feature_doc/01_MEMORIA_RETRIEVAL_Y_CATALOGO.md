---
tipo: especificacion_tecnica
proyecto: Cuadra
modulo: ai_memoria_retrieval
estado: DISEÑO APROBADO — listo para implementar
revisado: 2026-11-03
aprobado: 2026-11-03
revision: 2
tags: [memoria, retrieval, fastembed, fts5, turnos, context-budget, embeddings]
---

# 01 — Memoria, retrieval híbrido y manejo de ráfagas

> **Cambios de esta revisión:** el `TurnAssembler` en memoria se sustituye por turnos
> persistentes; el resumen usa id de mensaje, no conteo; FTS5 usa columnas reales;
> se crea `product_embeddings`; el modelo de embeddings es multilingüe; y el catálogo
> devuelve zonas como lista desde `supplier_delivery_zones`.

---

## 1. Diagnóstico (corregido)

| Problema | Estado real hoy | Consecuencia |
|---|---|---|
| Ráfagas | `ingest.py` encola un `generate_reply` por mensaje. El worker es único, así que no hay concurrencia real, pero sí **3 respuestas seguidas** y contexto desfasado. | El cliente percibe que el bot “no escucha”. |
| Lock por conversación | No existe; el worker único serializa todo, pero no garantiza que un turno vea el estado final de la ráfaga. | Respuestas intermedias. |
| Retrieval | `difflib` + solapamiento de tokens en `catalog.search_products`. Con 32 productos funciona; con cientos no entiende sinónimos. | “La batería grande” no encuentra “Power Bank 10000mAh”. |
| Resumen | `maybe_summarize` re-resume **todo** el historial viejo cada 10 mensajes. | Crece sin límite; a los 40 mensajes el prompt de resumen se come la ventana. |
| Estado | `pending_escalation`, `last_intent`, `last_generator` se guardan en `context_json` pero **no se inyectan** al prompt. | El modelo no sabe qué prometió. |
| Conversación | `WAITING_TCP` y `CLOSED` existen pero no se usan; la conversación abierta es eterna. | Contexto contaminado por productos viejos. |

> El doc original decía “1,000 productos colapsan”. Es una proyección, no una medición:
> hoy hay 32. La inversión en retrieval se justifica por el crecimiento esperado y por
> la calidad semántica, no porque hoy esté roto.

---

## 2. Turnos persistentes (reemplazo del `TurnAssembler` en memoria)

### 2.1 Por qué no usar `asyncio.TimerHandle`

La propuesta original guardaba los mensajes en un diccionario en RAM y usaba un timer
de 2.5 s. En una laptop con apagones frecuentes, eso pierde la ráfaga: los mensajes
están en `messages`, pero el job nunca se encola y el cliente queda sin respuesta.

Además, el buffer se indexaba por `customer_id`, no por conversación; dos canales del
mismo cliente se mezclarían.

### 2.2 Diseño: `turns` en SQLite + job con `due_at`

```sql
-- migrations/012_observability_and_turns.sql
CREATE TABLE IF NOT EXISTS turns (
    id                INTEGER PRIMARY KEY,
    conversation_id   INTEGER NOT NULL REFERENCES conversations(id),
    status            TEXT NOT NULL DEFAULT 'OPEN'
                      CHECK (status IN ('OPEN', 'PROCESSING', 'DONE', 'CANCELLED')),
    first_message_id  INTEGER,
    last_message_id   INTEGER,
    message_ids_json  TEXT NOT NULL DEFAULT '[]',
    due_at            TEXT NOT NULL,
    job_id            INTEGER,
    created_at        TEXT DEFAULT (datetime('now')),
    updated_at        TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_turns_due ON turns(status, due_at);
```

Flujo:

```text
1. Llega mensaje
   └─► messages.add_message(...)  (con external_id para dedupe)
   └─► turns.upsert_open_turn(conversation_id, message_id, due_at = now + debounce)
        ├─ si el turno ya existe: actualiza due_at y message_ids
        └─ si no: crea turno OPEN

2. Se encola `process_turn` con available_at = due_at
   └─ si el turno ya tiene job PENDING, se reprograma ese job (no se crea otro)

3. Worker reclama el job cuando vence `due_at`
   └─ turns.claim(turn_id)  -> OPEN a PROCESSING (atómico)
   └─ si ya está PROCESSING/DONE: no-op (idempotencia)
   └─ si llegó un mensaje nuevo y due_at cambió: reprograma y sale
   └─ si hay audio sin transcribir: encola transcripción y sale
   └─ pipeline.handle_turn(...)
   └─ turns.mark_done(turn_id)
```

**Ventajas:**

- Sobrevive apagones: al arrancar, `turns.recover_open()` reencola los `OPEN`.
- Un turno = una respuesta, aunque lleguen 3 mensajes.
- El `job_id` evita duplicar jobs.
- El estado `PROCESSING` evita que dos jobs procesen el mismo turno.

### 2.3 Contrato con la cola

`ai/queue.py` ya filtra por `next_retry_at <= now` en `_claim`. Se añade un parámetro
opcional a `enqueue`:

```python
def enqueue(job_type, payload, *, priority=100, available_at=None) -> int:
    ...
    # INSERT INTO jobs (..., next_retry_at) VALUES (..., available_at)
```

Y un helper para reprogramar el job de un turno:

```python
def reschedule(job_id: int, available_at: str) -> None:
    conn.execute(
        "UPDATE jobs SET next_retry_at = ?, updated_at = ? WHERE id = ? AND status = 'PENDING'",
        (available_at, _now(), job_id),
    )
```

No se usa `asyncio.Lock` por conversación: con un solo worker, el estado `PROCESSING`
del turno es suficiente. Si algún día hay más de un worker, se añade un lock por
`conversation_id` a nivel de BD (`turns` ya lo hace).

### 2.4 Configuración

```text
CUADRA_TURN_DEBOUNCE_S = 2.5      # segundos de silencio antes de procesar
CUADRA_TURN_MAX_WAIT_S  = 8.0      # si el cliente no para de escribir, no esperar eterno
CUADRA_HISTORY_WINDOW   = 8        # mensajes exactos en el prompt
CUADRA_SUMMARY_EVERY    = 8        # mensajes nuevos antes de resumir
```

### 2.5 Audio dentro de una ráfaga

- El audio se guarda y se encola `transcribe_audio` con `turn_id`.
- `process_turn` no procesa el turno hasta que todos los mensajes de audio del turno
  tengan `body`.
- Si la transcripción falla, el turno se marca `NEEDS_OPERATOR` (no se inventa texto).
- Para el TCP, el audio se transcribe antes de `tcp_process` (ver doc 03).

### 2.6 Criterio de aceptación

- 3 mensajes en 2 s → **1 turno**, **1 respuesta**, 1–2 llamadas al LLM
  (NLU + composición si hace falta). No “1 llamada” como decía el doc original.
- Reiniciar el proceso a mitad del debounce → el turno se recupera y responde una vez.
- Un mensaje duplicado por Telegram (`external_id` repetido) no crea otro turno.

---

## 3. Retrieval híbrido

### 3.1 Capa léxica: FTS5 con columnas reales

`products` tiene: `id`, `supplier_id`, `category_id`, `name`, `description`, `price`,
`currency`, `commission`, `commission_currency`, `stock`, `status`,
`specifications_json`, `created_at`, `updated_at`. **No tiene `brand` ni `category`.**

```sql
-- migrations/013_memory_retrieval.sql
CREATE VIRTUAL TABLE IF NOT EXISTS products_fts USING fts5(
    product_id UNINDEXED,
    name,
    description,
    category,
    specifications,
    tokenize = 'unicode61 remove_diacritics 2'
);

-- Backfill de los productos existentes (sin esto, FTS nace vacío)
INSERT INTO products_fts (product_id, name, description, category, specifications)
SELECT
    p.id,
    p.name,
    COALESCE(p.description, ''),
    COALESCE(c.name, ''),
    p.specifications_json
FROM products p
LEFT JOIN categories c ON c.id = p.category_id;

-- Triggers: delete + insert es más seguro que UPDATE en FTS5
CREATE TRIGGER IF NOT EXISTS trg_products_fts_ai AFTER INSERT ON products
BEGIN
    INSERT INTO products_fts (product_id, name, description, category, specifications)
    VALUES (
        new.id,
        new.name,
        COALESCE(new.description, ''),
        COALESCE((SELECT name FROM categories WHERE id = new.category_id), ''),
        new.specifications_json
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_products_fts_au AFTER UPDATE ON products
BEGIN
    DELETE FROM products_fts WHERE product_id = old.id;
    INSERT INTO products_fts (product_id, name, description, category, specifications)
    VALUES (
        new.id,
        new.name,
        COALESCE(new.description, ''),
        COALESCE((SELECT name FROM categories WHERE id = new.category_id), ''),
        new.specifications_json
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_products_fts_ad AFTER DELETE ON products
BEGIN
    DELETE FROM products_fts WHERE product_id = old.id;
END;
```

Notas:

- SQLite 3.49.1 (el del venv) soporta FTS5.
- `unicode61 remove_diacritics 2` hace que “batería” encuentre “bateria”.
- Los productos no se borran físicamente (soft delete con `status='REMOVED'`), así que
  el trigger de delete casi nunca se usa; se mantiene por completitud.
- Si en el futuro se añade `brand`, se indexa como parte de `specifications` o se crea
  la columna real; no se inventa en el trigger.

### 3.2 Capa semántica: FastEmbed en CPU

- Modelo: `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (384 dims,
  multilingüe, sin prefijos). **No** `bge-small-en-v1.5`, que es inglés.
- Runtime: ONNX Runtime en CPU con `providers=["CPUExecutionProvider"]` y 4 hilos.
- El modelo se descarga **una vez** y se guarda en `backend/models/fastembed/`.
  El arranque normal no toca Hugging Face (requisito offline).
- Si el modelo no está, el sistema arranca en modo degradado: FTS5 + fuzzy actual.

```sql
CREATE TABLE IF NOT EXISTS product_embeddings (
    product_id  INTEGER PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
    model       TEXT NOT NULL,
    dim         INTEGER NOT NULL,
    vector      BLOB NOT NULL,      -- float32 little-endian
    text_hash   TEXT NOT NULL,      -- para no recalcular si no cambió el texto
    updated_at  TEXT DEFAULT (datetime('now'))
);
```

Reindexado:

- Job `reindex_product` cuando cambia `name`, `description`, `category_id` o
  `specifications_json` (incluye mutaciones del TCP).
- Al arrancar: si faltan embeddings, se encola un `reindex_all_products` de baja
  prioridad; mientras tanto, búsqueda fuzzy.
- `text_hash` evita recalcular si el texto no cambió.

### 3.3 Fusión y carga de resultados

```text
query
 ├─ FTS5 top 20 (BM25)
 └─ embeddings top 20 (coseno en RAM)
        └─ Reciprocal Rank Fusion (k=60)
              └─ top 4
                    └─ cargar productos reales preservando el orden
                          └─ incluir ACTIVE y OUT_OF_STOCK
                                └─ catalog.llm_product_json(...)
```

Correcciones importantes frente al doc original:

- **No filtrar `stock > 0`**: un producto agotado debe poder responderse como agotado.
  Se incluyen `ACTIVE` y `OUT_OF_STOCK`; se excluyen `PAUSED` y `REMOVED`.
- **Preservar el orden** de RRF al cargar de SQLite: `WHERE id IN (...)` no garantiza
  orden; se reordena en Python.
- **No duplicar** el JSON seguro: se usa `catalog.llm_product_json`, que ya excluye
  comisión y datos internos.
- **Zonas:** `llm_product_json` debe devolver `zonas_entrega` como lista desde
  `supplier_delivery_zones` (con fallback a `suppliers.delivery_zone` legado), no un
  único `delivery_zone` de producto que no existe. Se conserva `zona_entrega` como
  texto para las plantillas actuales (`", ".join(...)`) para no romperlas.

```python
# ai/retrieval.py — forma, no implementación final
def search(conn, query: str, *, top_k: int = 4) -> list[dict]:
    lexical = _fts_search(conn, query, limit=20)      # [(product_id, rank)]
    semantic = _vector_search(conn, query, limit=20)  # [(product_id, rank)]
    fused = _rrf(lexical, semantic, k=60)[:top_k]
    products = [catalog.get_product(conn, pid) for pid in fused]
    return [
        {**catalog.llm_product_json(conn, p), "match_score": score}
        for p, score in products
        if p["status"] in ("ACTIVE", "OUT_OF_STOCK")
    ]
```

### 3.4 Fallback

Si FTS5 o embeddings fallan, se usa `catalog.search_products` (el fuzzy actual). Esto
mantiene el bot operativo en una laptop recién instalada o sin modelo de embeddings.

---

## 4. Memoria por capas

```text
┌────────────────────────────────────────────────────────────┐
│ CAPA 4: Perfil de cliente (customers.preferences_json)     │
│ - nombre habitual, zona, dirección, teléfono, notas        │
│ - compras anteriores (tabla sales)                         │
├────────────────────────────────────────────────────────────┤
│ CAPA 3: Resumen incremental (conversations.context_json)   │
│ - acuerdos de turnos antiguos, máximo 3 líneas             │
├────────────────────────────────────────────────────────────┤
│ CAPA 2: Hechos fijados (conversation_facts)                │
│ - zona, nombre, presupuesto, objeción, producto            │
├────────────────────────────────────────────────────────────┤
│ CAPA 1: Working buffer (messages)                          │
│ - últimos 8 mensajes exactos                               │
└────────────────────────────────────────────────────────────┘
```

### 4.1 Hechos fijados: `conversation_facts`

```sql
CREATE TABLE IF NOT EXISTS conversation_facts (
    id                 INTEGER PRIMARY KEY,
    conversation_id    INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    fact_key           TEXT NOT NULL,        -- 'zone_name', 'customer_name', 'budget'
    fact_value         TEXT NOT NULL,
    confidence         REAL NOT NULL DEFAULT 1.0,
    source_message_id  INTEGER REFERENCES messages(id),
    updated_at         TEXT DEFAULT (datetime('now')),
    UNIQUE (conversation_id, fact_key) ON CONFLICT REPLACE
);
```

Correcciones:

- No es “inmutable”: es el **valor vigente** por clave. El historial está en `events`
  y en los mensajes. Llamarlo inmutable era engañoso.
- `source_message_id` permite auditar de dónde salió el dato.
- `ON CONFLICT REPLACE` actualiza el valor; no crece indefinidamente.
- Los hechos **nunca se recortan** del contexto, aunque el presupuesto esté ajustado.

### 4.2 Resumen incremental

El resumen vive en `conversations.context_json` (no hay columnas `summary` en la tabla).
Se añade `summary_through_message_id` para saber exactamente qué se resumió.

```python
# ai/memory.py — forma, no implementación final
async def maybe_summarize(conn, conversation, llm_client):
    context = conversations.get_context(conn, conversation["id"])
    last_summarized_id = int(context.get("summary_through_message_id") or 0)

    rows = conn.execute(
        """SELECT id, direction, author, body, type
           FROM messages
           WHERE conversation_id = ? AND id > ?
           ORDER BY id""",
        (conversation["id"], last_summarized_id),
    ).fetchall()

    if len(rows) <= config.HISTORY_WINDOW + config.SUMMARY_EVERY:
        return  # todavía no hay suficiente historial viejo

    # Todo menos los últimos HISTORY_WINDOW mensajes
    delta = rows[:-config.HISTORY_WINDOW]
    if not delta:
        return

    new_summary = await llm_client.summarize(
        previous_summary=context.get("summary"),
        messages=[dict(r) for r in delta],
    )
    context["summary"] = new_summary.strip()
    context["summary_through_message_id"] = delta[-1]["id"]
    conversations.set_context(conn, conversation["id"], context)
```

Reglas:

- Se resume **solo el delta**, nunca todo el historial.
- El límite es 3 líneas / ~120 tokens.
- Si el resumen falla, no se rompe la respuesta: se conserva el anterior y se reintenta
  en el siguiente turno.
- `summary_through_message_id` es un id, no un conteo. Esto evita saltarse mensajes
  cuando hay huecos de ids.
- No se resume un turno que todavía está `PROCESSING`.

### 4.3 Perfil de cliente (memoria entre conversaciones)

No hace falta una tabla nueva. Se usa:

| Fuente | Uso |
|---|---|
| `customers.preferences_json` | `{name, usual_zone, usual_address, phone, notes}` actualizado desde ventas confirmadas. |
| `sales` | Últimas 3 compras: producto, zona, fecha, estado. |
| `conversation_facts` de conversaciones cerradas | Solo si el cliente vuelve y se necesita un dato histórico. |

Al abrir una conversación nueva, `ContextBuilder` inyecta:

```json
{
  "perfil": {
    "nombre": "Carlos",
    "zona_habitual": "Marianao",
    "telefono": "+53 52345678",
    "compras_anteriores": 2
  }
}
```

El bot puede saludar con contexto (“¿Te llevo otra vez a Marianao?”) sin pedir todo
de nuevo. Si el cliente cambia de zona, se actualiza el perfil.

### 4.4 Cierre de conversaciones

- Scheduler diario cierra conversaciones sin mensajes durante
  `CUADRA_CONVERSATION_IDLE_HOURS=72`.
- `status='CLOSED'`, `closed_at=now`. La columna `closed_at` **no existe** en
  `conversations`; se añade en `013_memory_retrieval.sql`.
- Al volver, se crea una conversación nueva; el perfil y las compras siguen disponibles.
- `WAITING_TCP` se usa cuando hay una escalación abierta; no impide recibir mensajes
  nuevos del cliente, solo marca el estado para el dashboard.

---

## 5. Presupuesto de contexto

El doc original fijaba 4096 tokens y repartía el prompt en una tabla. El proyecto usa
`CUADRA_LLM_CTX=8192`; el presupuesto debe **derivarse** de la configuración:

```text
prompt_budget = LLM_CTX - max_output_tokens - safety_margin
```

Ejemplo con `LLM_CTX=8192`, `max_output_tokens=600`, `safety_margin=400`:
`prompt_budget ≈ 7192`. Con `LLM_CTX=4096`: `prompt_budget ≈ 3096`.

Orden de prioridad (nunca se recorta lo de arriba):

| Prioridad | Segmento | Tokens objetivo | Recorte |
|---|---|---|---|
| 1 | System prompt + reglas | 600–800 | Nunca. |
| 2 | Estado + hechos fijados | 150–250 | Nunca. |
| 3 | Venta/escalación abierta | 100–150 | Nunca. |
| 4 | Productos recuperados (top 3) | 400–700 | Bajar a 2 productos. |
| 5 | Resumen incremental | 100–150 | Nunca por debajo de 80. |
| 6 | Historial reciente | 600–900 | Bajar de 8 a 4 mensajes. |
| 7 | Turno actual | 150–300 | Truncar texto muy largo. |
| 8 | Tarea / esquema JSON | 300–450 | Nunca. |
| 9 | Margen de generación | 600 | Configurable. |
| 10 | Buffer de seguridad | 300–400 | Ajustar según medición. |

Reglas:

- Se recorta primero el historial, luego los productos, luego el turno. **Nunca** los
  hechos fijados ni las reglas.
- La estimación actual (`json.dumps` / 4) es optimista en español. Se usa 3.5
  caracteres/token y se valida con el tokenizer real en la Fase 0. Si el modelo expone
  `/tokenize`, se puede usar para calibrar, no en cada turno.
- `LLM_MAX_PROMPT_TOKENS` deja de ser un número independiente: se calcula a partir de
  `LLM_CTX` y `max_output_tokens`, o se valida contra ellos.

---

## 6. Qué se mide

| Métrica | Cómo |
|---|---|
| Ráfagas | 3 mensajes → 1 turno/1 respuesta; log de `turns`. |
| Memoria | Tras 50 mensajes, ¿siguen producto/zona/presupuesto en el prompt? |
| Resumen | Tokens del prompt de resumen; no debe crecer con el historial. |
| Retrieval | Recall@3 en 50 consultas reales; tiempo de búsqueda. |
| Contexto | Tokens reales vs presupuesto; % de recortes. |
| Latencia | p50/p95 por turno. |
| Fallback | % de turnos que usan fuzzy porque FTS/embeddings fallaron. |

---

## 7. Lo que NO se hace en esta fase

- No se añade `sqlite-vec`; con 2,500 productos la matriz en RAM es suficiente.
- No se usa Chroma/LanceDB/Qdrant (procesos extra).
- No se usa Mem0/Letta/Zep (servidores y dependencias que no caben en el objetivo).
- No se promete “< 3 ms” de búsqueda semántica; se mide.
- No se vectorizan conversaciones completas; solo productos y, si hace falta, hechos.
