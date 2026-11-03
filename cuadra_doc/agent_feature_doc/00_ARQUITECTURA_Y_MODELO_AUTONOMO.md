---
tipo: arquitectura_sistema
proyecto: Cuadra
modulo: core_y_autonomia
estado: DISEÑO APROBADO — listo para implementar
revisado: 2026-11-03
aprobado: 2026-11-03
revision: 2
tags: [arquitectura, ai-agent, autonomia-configurable, finanzas, comisiones, multicanal]
---

# 00 — Arquitectura del sistema y modelo de autonomía

> **Cambios de esta revisión:** se corrigió el modelo asumido, se alineó el esquema con
> `001_init.sql`–`011_broadcast_publishing.sql`, se sustituyó la “autonomía optimista
> obligatoria” por **autonomía configurable con reversión**, y se unificaron los estados
> de conversación y venta. Ver `05_VIABILIDAD_Y_CAMBIOS_REQUERIDOS.md`.

---

## 1. Filosofía: autonomía configurable, no autonomía ciega

El sistema anterior fallaba por dos extremos: plantillas rígidas sin iniciativa, o un
operador aprobando cada paso. La propuesta original resolvía esto con “autonomía
optimista”: el bot crea ventas, despacha al TCP y muta el catálogo sin preguntar.

Eso es **viable**, pero con un modelo local de 3B/4B, audio de WhatsApp/Telegram y un
catálogo que mueve dinero, la autonomía total desde el día uno es un riesgo innecesario.
Cuadra adopta **autonomía configurable**:

| Modo | Qué hace el bot | Qué hace el operador | Cuándo usarlo |
|---|---|---|---|
| `assisted` (default) | Captura datos, crea la venta en `PENDING_APPROVAL`, prepara el despacho. | Aprueba con un clic (dashboard/Telegram); el bot despacha. | Piloto y primeras semanas. |
| `hybrid` | Si el producto tiene stock, precio y zona confirmados, crea y despacha solo. Si falta algo, pasa a asistido. | Solo interviene en excepciones. | Operación estable con datos confiables. |
| `autonomous` | Crea la venta en `PENDING_DISPATCH` y despacha al TCP sin aprobación. | Audita, cobra comisiones y revierte errores. | Cuando las métricas lo justifiquen. |

La autonomía se controla por settings, no por código duplicado:

```text
sales.mode = assisted | hybrid | autonomous
catalog.autoapply.stock = 1
catalog.autoapply.specs = 1
catalog.autoapply.price = 1          # auto con límites; fuera de rango va a revisión
catalog.price_max_delta_pct = 50      # ±50% para auto-aplicar
catalog.autoapply.min_confidence = 0.85
```

> **Decisión aprobada (D1/D2):** el sistema arranca en `assisted` y pasa a
> `autonomous` cuando se cumplan las métricas de aceptación; el precio se auto-aplica
> dentro de los límites anteriores y, si no, va a revisión.

**Regla de oro:** toda mutación autónoma deja un `catalog_mutation_log` con estado
anterior y nuevo. Si el bot interpretó mal al TCP, el operador revierte con un clic.
Pero el undo **no es magia**: si otra mutación cambió el producto después, el revert
se rechaza con `409` en vez de pisar datos buenos.

---

## 2. Flujo operativo objetivo

```text
[CLIENTE] ──► negocia/cierra ──► [BOT CUADRA] ──► orden ──► [TCP / ALMACÉN]
                                      │                         │
                                      ├─ venta + reserva        │ entrega y cobra
                                      ├─ ledger de comisión     ▼
                                      ▼                     [CLIENTE]
                              [AUDITORÍA / DASHBOARD]
                                      │
                                      ▼
                              [OPERADOR: audita, cobra comisión, revierte]
```

En modo `assisted` hay un paso extra: el operador aprueba la venta antes del despacho.
En modo `autonomous` ese paso desaparece, pero el operador sigue viendo todo y puede
cancelar antes de la entrega.

---

## 3. Aislamiento multicanal

El dominio comercial y la IA no deben saber si el mensaje viene de Telegram, WhatsApp
o un endpoint de pruebas. El proyecto ya tiene esa separación en `backend/cuadra/channels/`;
la propuesta la refuerza, no la reescribe.

### 3.1 Normalización de entrada

```text
Telegram polling (aiogram) ─┐
Telegram userbot (Telethon) ─┼─► channels/* ─► ai/ingest.py ─► turns (SQLite)
WhatsApp Fase 2 ────────────┘                         │
                                                      ▼
                                            ai/orchestrator.py
                                                      │
                                                      ▼
                                            queue (jobs) ─► send_message
```

`ai/ingest.py` sigue siendo el único punto de entrada. Se le añade:

- `external_id` para deduplicar (Telegram `message_id`/`update_id`; WhatsApp id).
- Creación/actualización de un **turno** persistente (`turns`) con debounce.
- Distinción cliente/TCP ya existente por `suppliers.telegram_id`.

### 3.2 Estructuras canónicas

No se crea un `domain/messages.py` paralelo al esquema. El contrato se mantiene en
`messages` (tabla real) y se añade un dataclass ligero solo para el turno:

```python
# backend/cuadra/ai/turn.py
from dataclasses import dataclass, field
from datetime import datetime

@dataclass
class Turn:
    id: int
    conversation_id: int
    channel: str
    message_ids: list[int]
    consolidated_text: str
    due_at: datetime
    status: str = "OPEN"  # OPEN | PROCESSING | DONE | CANCELLED
```

El mapeo con la BD es directo:

| Concepto propuesto | Columna real |
|---|---|
| `channel` | `conversations.channel` (`telegram`/`whatsapp`) |
| `author_role` | `messages.author` (`customer`/`bot`/`tcp`/`operator`) |
| `channel_message_id` | **nuevo** `messages.external_id` |
| `text` | `messages.body` |
| `audio_bytes` | `messages.file_path` (se guarda en disco, no en BD) |
| `timestamp` | `messages.created_at` |

No se añade `system` a `messages.author`; los eventos del sistema van a `events`.

---

## 4. Modularidad del núcleo IA

Se mantienen los nombres existentes para no romper imports. Los módulos nuevos se
añaden alrededor, no en lugar de:

```text
backend/cuadra/ai/
├── __init__.py
├── ingest.py          # entrada, dedupe, crea/actualiza turnos
├── assembler.py       # NUEVO: debounce persistente sobre `turns`
├── orchestrator.py    # NUEVO: máquina de estados + acciones deterministas
├── context.py         # NUEVO: ContextBuilder con presupuesto
├── retrieval.py       # NUEVO: FTS5 + embeddings + fallback fuzzy
├── memory.py          # NUEVO: hechos, resumen incremental, perfil
├── guardrails.py      # NUEVO: precios permitidos, términos internos, longitud
├── llm.py             # EXISTENTE: cliente llama.cpp/Ollama + contratos
├── prompts.py         # EXISTENTE: persona, tareas y plantillas
├── stt.py             # EXISTENTE: faster-whisper
├── queue.py           # EXISTENTE: cola persistente
└── handlers.py        # EXISTENTE: jobs; se añaden process_turn/tcp_process
```

**No se renombra** `llm.py` a `llm_client.py` ni `prompts.py` a `prompts/`. Ese cambio
solo añade ruido y rompe imports. Si más adelante los prompts crecen, se migra a
Jinja2 con una decisión explícita (ver D9 en doc 05).

Módulos de dominio nuevos:

```text
backend/cuadra/domain/
├── turns.py            # NUEVO: CRUD de turnos + recuperación al arrancar
├── sales.py            # NUEVO: venta, reserva, entrega, cancelación
├── finance.py          # NUEVO: ledger y métricas por moneda
└── catalog_mutations.py# NUEVO: mutación atómica + undo con conflicto
```

---

## 5. Hardware y entorno real

| Elemento | Realidad hoy | Propuesta |
|---|---|---|
| Host | Windows, 32 GB RAM | Igual. |
| GPU | RTX 3050 Laptop, 4096 MiB | Igual. |
| Motor | `llama-server.exe` propio, `--jinja`, `-ngl 99`, `-np 1` | Igual. |
| Contexto | `CUADRA_LLM_CTX=8192` por defecto | Medir 4096 vs 8192; no asumir. |
| Modelo actual | `Bonsai-8B-Q1_0.gguf` (1.08 GB) | Candidato a reemplazo. |
| Modelo ya descargado | `NVIDIA-Nemotron-3-Nano-4B-Q4_K_M.gguf` (2.64 GB) | Candidato inmediato. |
| Modelo propuesto por el doc original | `Qwen2.5-3B-Instruct-Q5_K_M` | **No está descargado**; candidato a bake-off. |
| STT | faster-whisper `small` int8, CPU, 4 hilos | Igual. |
| Embeddings | No implementados | FastEmbed CPU, modelo multilingüe vendorizado. |

**No se afirma que Qwen esté en ejecución.** La selección de modelo es una decisión
basada en medición (doc 05, D8). El bake-off debe comparar, como mínimo:

- `Bonsai-8B-Q1_0` (línea base).
- `NVIDIA-Nemotron-3-Nano-4B-Q4_K_M` (ya disponible).
- Un Qwen 3B/4B Q4/Q5 y, si cabe, un Gemma 3 4B Q4.

Métricas: VRAM real (`ai/llm_memory.py`), TTFT, tokens/s, precisión de intención,
calidad de redacción, resistencia a inyección y fidelidad al contexto.

> **Nota de contexto:** el doc original asignaba `-c 4096`. El proyecto usa 8192.
> Bajar a 4096 reduce VRAM pero también el espacio para historial + producto + tarea.
> No se cambia hasta medir el modelo elegido.

---

## 6. Dominio nuevo: ventas, comisiones y control del operador

El operador deja de ser telefonista y pasa a ser **administrador financiero del
comisionista**. Pero el bot no “cobra”: el TCP entrega y cobra al cliente; Cuadra
luego le cobra su comisión al TCP.

### 6.1 Ciclo de vida de una venta

```text
[DRAFT]                         (opcional; el bot normalmente no lo usa)
   │
   ▼
[PENDING_APPROVAL]              (modo assisted: espera al operador)
   │  aprobación
   ▼
[PENDING_DISPATCH]              (lista para enviar al TCP)
   │  send ok
   ▼
[DISPATCHED]                    (TCP recibió la orden)
   │  TCP acepta
   ▼
[CONFIRMED]                     (TCP confirma que la prepara)
   │  TCP entrega y cobra al cliente
   ▼
[DELIVERED]                     (stock físico descontado; comisión por cobrar)
   │  operador recibe la comisión
   ▼
[SETTLED]                       (ledger COLLECTED)

Cancelaciones: CANCELLED (antes de entregar) | NEEDS_OPERATOR (ambigüedad/rechazo)
```

En modo `autonomous`, el bot salta de `PENDING_APPROVAL` a `PENDING_DISPATCH`
directamente. En modo `hybrid`, decide según stock/precio/zona.

### 6.2 Qué guarda cada tabla

| Tabla | Responsabilidad |
|---|---|
| `sales` | Ciclo operativo de la orden, datos de entrega, snapshot de precio/comisión. |
| `commission_ledger` | Solo lo financiero: comisión ganada, cobrada o anulada. No duplica estados de entrega. |
| `catalog_mutation_log` | Auditoría y undo de cambios de catálogo. |
| `conversation_facts` | Datos fijados del cliente (zona, nombre, presupuesto…). |
| `turns` | Ráfagas y procesamiento idempotente. |
| `llm_calls` | Trazas de prompt/completion para evaluación y depuración. |

### 6.3 Esquema financiero (migración `014`)

La propuesta original repartía el esquema entre dos migraciones, duplicaba
`commission_ledger` y usaba `ALTER TABLE ... ADD COLUMN ... UNIQUE`, que SQLite no
permite. La versión corregida vive en **una sola migración** `014_sales_and_finance.sql`
y reconstruye `sales` porque hay que cambiar su `CHECK` de estados.

Resumen del DDL (el detalle completo está en doc 02):

```sql
-- products: stock físico vs. reservado y tipo de comisión
ALTER TABLE products ADD COLUMN stock_reserved INTEGER NOT NULL DEFAULT 0;
ALTER TABLE products ADD COLUMN commission_type TEXT NOT NULL DEFAULT 'FIXED'
    CHECK (commission_type IN ('FIXED', 'PERCENT'));

-- sales: se reconstruye (PRAGMA foreign_keys=OFF + CREATE new + copy + rename)
-- estados: DRAFT, PENDING_APPROVAL, PENDING_DISPATCH, DISPATCHED,
--          CONFIRMED, DELIVERED, SETTLED, CANCELLED, NEEDS_OPERATOR
-- nuevas columnas: order_code, quantity, recipient_name, delivery_zone,
--                  delivery_zone_id, delivery_address, customer_phone,
--                  commission_currency, dispatched_at, tcp_ack_at,
--                  delivered_at, settled_at, cancelled_at, notes

CREATE UNIQUE INDEX idx_sales_order_code
    ON sales(order_code) WHERE order_code IS NOT NULL;

-- ledger financiero, sin duplicar el ciclo de entrega
CREATE TABLE commission_ledger (
    id                 INTEGER PRIMARY KEY,
    sale_id            INTEGER NOT NULL UNIQUE REFERENCES sales(id),
    product_id         INTEGER NOT NULL REFERENCES products(id),
    supplier_id        INTEGER NOT NULL REFERENCES suppliers(id),
    product_name       TEXT NOT NULL,
    sale_total         REAL NOT NULL,
    currency           TEXT NOT NULL CHECK (currency IN ('USD', 'CUP')),
    commission_type    TEXT NOT NULL CHECK (commission_type IN ('FIXED', 'PERCENT')),
    commission_rate    REAL NOT NULL,
    commission_earned  REAL NOT NULL,
    commission_currency TEXT NOT NULL CHECK (commission_currency IN ('USD', 'CUP')),
    supplier_payout    REAL,  -- solo si currency = commission_currency
    status             TEXT NOT NULL DEFAULT 'PENDING'
                       CHECK (status IN ('PENDING', 'COLLECTED', 'VOID')),
    collected_at       TEXT,
    notes              TEXT,
    created_at         TEXT DEFAULT (datetime('now'))
);
```

**Decisiones de diseño:**

- `order_code` se genera **después** del insert: `V-{id:06d}`. No se usan 4 dígitos
  aleatorios (colisionan).
- `sales.amount` es el total; `sales.commission` es la comisión snapshot con
  `commission_currency`; el ledger conserva ambos.
- Si `currency != commission_currency`, `supplier_payout` queda `NULL` y el dashboard
  muestra “requiere conversión”. No se suman USD y CUP en una misma métrica.
- `stock_reserved` separa stock físico de reservas. Al vender se reserva; al entregar
  se descuenta el físico; al cancelar se libera.
- `sales.status=SETTLED` significa “Cuadra ya cobró su comisión”, no “el cliente pagó”.
  El cliente paga al TCP en la entrega (`DELIVERED`).

> **Decisión pendiente D4:** si la comisión puede ser en moneda distinta a la venta.
> El esquema la soporta, pero la política de conversión la define el dueño.

---

## 7. Máquina de estados de la conversación

El vocabulario debe ser único en todo el proyecto. El código usa inglés; la UI traduce.

```text
DISCOVERY → PRODUCT_MATCH → QUOTE → OBJECTION → CLOSE
   → CAPTURE_DATA → CONFIRM → DONE
   ↘ HANDOFF (operador o TCP) ↗
```

| Estado | Qué significa | Acción del bot |
|---|---|---|
| `DISCOVERY` | Saludo o necesidad vaga. | Preguntar qué busca; retrieval amplio. |
| `PRODUCT_MATCH` | Hay producto(s) candidato(s). | Mostrar precio/stock/zona reales. |
| `QUOTE` | Cliente interesado en un producto. | Cotización con plantilla; resolver dudas. |
| `OBJECTION` | “Está caro”, “lo vi más barato”, “déjame pensarlo”. | Manejar objeción con datos reales; no inventar descuentos. |
| `CLOSE` | “Lo quiero”, “tráemelo”. | Pasar a captura de datos. |
| `CAPTURE_DATA` | Faltan nombre, zona, dirección o teléfono. | Pedir solo lo que falta, uno o dos datos por turno. |
| `CONFIRM` | Datos completos. | Resumen + confirmación explícita. |
| `DONE` | Venta creada o conversación cerrada. | Cierre; no seguir vendiendo. |
| `HANDOFF` | Ambigüedad, queja o petición humana. | Derivar al operador; no improvisar. |

El estado vive en `conversations.context_json`:

```json
{
  "stage": "CAPTURE_DATA",
  "product_id": 42,
  "quantity": 1,
  "quoted_price": 25.0,
  "quoted_currency": "USD",
  "zone_id": 13,
  "zone_name": "Marianao",
  "customer_name": "Carlos",
  "address": null,
  "phone": "+53 52345678",
  "budget": null,
  "objection": null,
  "last_offer": "25 USD con entrega en Marianao",
  "pending_escalation_id": null,
  "open_sale_id": null,
  "handoff_reason": null,
  "summary": "…",
  "summary_through_message_id": 118
}
```

El estado **sí** se inyecta al prompt. Hoy `pending_escalation` y `last_intent` se
guardan pero no se usan; eso se corrige en `ContextBuilder`.

---

## 8. Módulo de guardrails

La propuesta original hablaba de “verificadores de precio” sin diseño. Se concreta:

```text
ai/guardrails.py
├── allowed_prices(context) -> set[Decimal]
│     precios de productos del turno, cotización, venta abierta y respuesta TCP
├── check_prices(reply, allowed) -> bool
│     cualquier cifra con USD/CUP debe estar en el conjunto permitido
├── check_internal_terms(reply) -> bool
│     bloquea comisión, margen, costo de compra, ganancia, liquidación, payout
├── check_length(reply) -> bool
│     máximo 3 párrafos / ~900 caracteres
└── check_format(reply) -> bool
      sin JSON, sin markdown de código, sin “soy un bot/IA”
```

Si una respuesta falla:

1. Se reintenta una vez con la instrucción de corregir.
2. Si vuelve a fallar, se usa una plantilla determinista del estado actual.
3. Se registra `guardrail_violation` en `events` con el motivo (sin el texto completo
   si contiene PII).

Esto es más importante que el prompt: un 3B puede “entender” la regla y violarla igual.

---

## 9. Idempotencia y apagones

Cuba tiene apagones frecuentes; el diseño debe asumirlo.

| Punto | Riesgo | Solución |
|---|---|---|
| Entrada | Telegram reprocesa `update` tras reinicio | `messages.external_id` único por conversación. |
| Ráfaga | Buffer en memoria se pierde | `turns` en SQLite + `due_at` + recuperación al arrancar. |
| Respuesta | Se genera dos veces | `process_turn` marca el turno `DONE`; el job duplicado no hace nada. |
| Envío | Se envía dos veces | `message_sent` event (ya existe). |
| Despacho TCP | Se envía dos veces al TCP | `sale_dispatched` event + estado `DISPATCHED`. |
| Mutación | Se aplica dos veces | `catalog_mutation_log` + `source_message_id` único. |
| Escalación | Se pregunta dos veces | `escalation_sent` event (ya existe). |

---

## 10. Ventajas del rediseño (corregidas)

1. **No se bloquea la operación:** en modo autónomo el bot vende y despacha sin ti;
   en modo asistido solo necesitas un clic.
2. **Control financiero real:** el dashboard muestra comisiones ganadas, cobradas y
   pendientes, separadas por moneda, sin sumar peras con manzanas.
3. **Red de seguridad ante alucinaciones:** el undo existe, pero además hay límites de
   precio, confianza mínima, revisión por ambigüedad y detección de conflicto.
4. **Sobrevive a apagones:** turnos, jobs y mutaciones son persistentes.
5. **Se puede medir:** `llm_calls` + golden set permiten saber si el cambio mejora.
6. **Respeta las decisiones cerradas:** el operador sigue cerrando el pago; el LLM
   nunca escribe SQL; el bot no inventa precio/stock/garantía.

---

## 11. Riesgos que esta arquitectura NO elimina

- Un modelo de 3B/4B sigue siendo limitado para conversaciones largas y objeciones.
- El audio de un TCP puede transcribirse mal; por eso el precio auto-aplicado exige
  correlación inequívoca, confianza ≥ 0.85 y cambio dentro de ±50 %.
- El operador debe mantener el CRUD de zonas: una zona mal creada o desactivada por
  error produce entregas imposibles o escalaciones.
- Una comisión en moneda distinta a la venta no se puede liquidar sin conversión; el
  dashboard lo marca, pero no la resuelve solo.
- La autonomía total sin métricas es una apuesta, no una mejora.

---

## 12. Referencias

- `05_VIABILIDAD_Y_CAMBIOS_REQUERIDOS.md` — discordancias, decisiones aprobadas y fases.
- `01_MEMORIA_RETRIEVAL_Y_CATALOGO.md` — turnos, memoria y retrieval.
- `02_MAQUINA_ESTADOS_VENTAS_Y_FINANZAS.md` — ventas, ledger y API.
- `03_BUCLE_TCP_MUTACION_CATALOGO_Y_ROLLBACK.md` — TCP, mutaciones y undo.
- `04_PLAN_IMPLEMENTACION_Y_PROMPTS_MAESTROS.md` — fases, contratos y pruebas.
- `backend/migrations/001_init.sql` … `011_broadcast_publishing.sql` — esquema real.
- `backend/cuadra/config.py` — knobs reales.
- `backend/cuadra/ai/pipeline.py` — pipeline actual.
