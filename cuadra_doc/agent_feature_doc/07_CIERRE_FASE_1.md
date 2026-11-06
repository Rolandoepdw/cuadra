---
tipo: cierre_fase
proyecto: Cuadra
modulo: agente_ia
estado: FASE 1 — implementada, pendiente medición p95
fecha: 2026-11-06
tags: [fase-1, turnos, idempotencia, guardrails, contexto]
---

# 07 — Cierre de la Fase 1

> Turnos persistentes, idempotencia de extremo a extremo, presupuesto de contexto y
> guardrails de salida. **Sin tocar la base real ni la operación**: las pruebas corren
> sobre bases temporales y el flujo HTTP se validó con un servidor OpenAI-compatible
> simulado.

---

## 1. Qué cambió

| Archivo | Cambio |
|---|---|
| `migrations/012_observability_and_turns.sql` | `turns` y `messages.external_id` (comparte migración con Fase 0). |
| `domain/turns.py` (nuevo) | Ciclo `OPEN → PROCESSING → DONE`/`CANCELLED`, `claim` atómico y recuperación al arranque. |
| `ai/assembler.py` (nuevo) | Debounce persistente: cada mensaje re-arma el mismo job; `TURN_MAX_WAIT_S` pone el techo. |
| `ai/handlers.py` | Job `process_turn`; audio antes de responder; recuperación de la respuesta ya guardada. |
| `ai/pipeline.py` | `handle_turn(conn, conversation, turn)` responde a la ráfaga completa, no al último mensaje. |
| `ai/context.py` (nuevo) | `ContextBuilder` con presupuesto derivado de `CUADRA_LLM_CTX`. |
| `ai/guardrails.py` (nuevo) | Precios permitidos, términos internos, longitud y formato; reintento y plantilla. |
| `ai/queue.py` | `enqueue(..., available_at=...)`, `reschedule(job_id, available_at)`. |
| `ai/ingest.py` | Deduplicación por `external_id`; crea/actualiza turno y arma `process_turn`. |
| `domain/conversations.py` | `add_message(..., external_id=...)`, `add_message_with_status`, `get_messages`. |
| Canales Telegram | Envían `message_id` como `external_id` (bot y userbot). |
| `main.py` | Al arrancar: `queue.requeue_in_progress()` y después `assembler.recover_open()`. |

Configuración nueva: `CUADRA_TURN_DEBOUNCE_S` (2.5 s), `CUADRA_TURN_MAX_WAIT_S` (8 s),
`CUADRA_LLM_MAX_OUTPUT_TOKENS` (600) y `CUADRA_LLM_CONTEXT_SAFETY_TOKENS` (400).

---

## 2. Ciclo del turno

```text
mensaje ──► upsert_open_turn ──► schedule_turn (job PENDING en due_at)
                                        │
                    nuevo mensaje ──────┘  re-arma el MISMO job
                                        │
                    process_turn despierta
                                        │
              due_at futuro ──► seguimiento (no responde a medias)
                                        │
              audio pendiente ──► transcribe_audio ──► re-arma el turno
                                        │
                          claim atómico OPEN → PROCESSING
                                        │
                     pipeline.handle_turn (ráfaga consolidada)
                                        │
                    respuesta + evento reply_queued
                                        │
                              mark_done → DONE
                                        │
                              job send_message
```

Reglas implementadas:

- `claim` es `UPDATE ... WHERE status = 'OPEN'`: un job duplicado es un no-op.
- Un mensaje que llega **antes** del vencimiento re-arma el mismo job; uno que llega
  **después del claim** crea un turno nuevo (recibe la siguiente respuesta).
- `recover_open()` devuelve `PROCESSING` huérfanos a `OPEN` y reencola los `OPEN` sin
  job vivo, respetando `due_at`.

---

## 3. Idempotencia y ventanas de caída

| Ventana de caída | Protección |
|---|---|
| Telegram reenvía el mismo update | Índice único `(conversation_id, external_id)`; no crea mensaje ni turno. |
| Caída durante el debounce | El turno y su `due_at` están en SQLite; `recover_open()` lo re-arma. |
| Caída después de reclamar, antes de responder | El job se reintenta; el turno vuelve a `OPEN` (`release`/`recover_open`). |
| Caída después de guardar la respuesta, antes de `reply_queued` | El mensaje saliente usa `external_id = "turn:<id>"`: el reintento reutiliza la misma fila. |
| Caída después de `reply_queued`, antes de `mark_done` | `process_turn` detecta la respuesta guardada, cierra el turno y solo reencola el envío. |
| Caída después de `mark_done`, antes de `send_message` | La misma comprobación reencola el envío sin regenerar la respuesta. |
| `send_message` ya entregado | El evento `message_sent` evita reenvíos. |
| Audio transcrito y reintentado | Si el mensaje ya tiene texto, no se vuelve a ejecutar STT. |

El envío perdido se recupera con el evento `reply_queued` (entidad `turns`) más la
comprobación de `message_sent` y de jobs `send_message` vivos.

---

## 4. Presupuesto de contexto

```text
prompt_budget = min(LLM_MAX_PROMPT_TOKENS, LLM_CTX - max_output - safety)
```

Con `LLM_CTX=8192`, salida 600 y margen 400: ~7 192 tokens; el tope explícito de
2 500 manda mientras no se suba. Orden de recorte:

1. Historial reciente (de 12 mensajes hacia abajo).
2. Productos candidatos (de N a 1).
3. Texto del turno actual (nunca por debajo de 200 caracteres).

Nunca se recortan el system prompt, el resumen, el estado de la conversación ni las
instrucciones de la tarea. El texto que se mide es el mismo que se envía al modelo.

---

## 5. Guardrails

| Regla | Comportamiento |
|---|---|
| Precios | Todo importe citado debe coincidir con un precio del contexto del cliente (±0.01). |
| Términos internos | Bloquea comisión, margen, costo, utilidad, ganancia, payout, liquidación… |
| Longitud | ≤ 900 caracteres, ≤ 3 párrafos, ≤ 6 líneas. |
| Formato | Sin JSON, sin bloques de código, sin encabezados markdown. |

Si falla: **un reintento correctivo** y, si persiste, **plantilla determinista**. Cada
intento deja un evento `guardrail_violation` con `reason` y `stage` (`retry`/`fallback`),
sin guardar el texto ofensivo.

---

## 6. Aceptación

| Criterio (doc 04, Fase 1) | Estado |
|---|---|
| 3 mensajes en 2 s → 1 turno, 1 respuesta, 1–2 llamadas NLU | ✅ Prueba unitaria + HTTP simulado (1 llamada `intent`, camino de plantilla). |
| Reinicio a mitad del debounce → responde una vez | ✅ `RecoveryTests` + `ReplyOutboxTests`. |
| `external_id` repetido → no crea turno nuevo | ✅ `BurstTests`. |
| 20 intentos de fuga de comisión → 0 fugas | ✅ `GuardrailTests`: 20 intentos que cubren 17 variantes, todos bloqueados. |
| p95 de latencia medido y registrado | ⏳ Pendiente: el servidor local no estaba levantado y el bake-off sigue pospuesto. |

Pruebas: `43` casos en `unittest` (24 de Fase 0 + 19 de Fase 1), todos en verde.

```powershell
cd backend
.venv\Scripts\python.exe -m unittest discover -s tests -q
```

Validación HTTP adicional (base temporal, servidor simulado):

- Ráfaga de 3 mensajes → 1 turno, 1 salida, 1 llamada `intent`, 0 eventos de guardrail.
- Respuesta del modelo con fuga (`comisión`/`costo`) → eventos `retry` y `fallback` →
  plantilla segura, sin filtrar cifras internas.

---

## 7. Limitaciones de esta fase

- **p95 sin medir**: falta levantar el modelo elegido y correr el flujo real; no se
  inventa una cifra con un servidor simulado.
- **Sin Telegram real**: los canales solo se ejercitaron por unit tests y HTTP local.
- **Resumen incremental**: sigue basado en conteo de mensajes; `summary_through` y los
  hechos llegan en Fase 2.
- **Retrieval**: `catalog.search_products` (difflib/tokens) sigue igual; FTS5 y
  embeddings son Fase 2.
- **NLU degradada**: si el modelo devuelve JSON inválido se registra `nlu_invalid_output`
  y se responde con intención `otro`; no hay reintento de clasificación todavía.
- **Cancelación de turnos**: `mark_cancelled` existe en el dominio, pero todavía no hay
  flujo que la invoque (borrado de mensaje o descarte manual).
- **Retención de turnos**: no hay purga de `turns`; se evaluará con datos de operación.

---

## 8. Próximo paso

1. Cuando el dueño levante el modelo, medir p95 real del flujo completo y anotarlo aquí.
2. Revisar el diff de Fase 1 con el dueño; **no se hace commit sin pedido explícito**.
3. Pasar a Fase 2 (memoria, hechos y retrieval) manteniendo `sales.mode` y los
   settings de autonomía como interruptores de comportamiento.
