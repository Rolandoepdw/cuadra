---
tipo: cierre_fase
proyecto: Cuadra
modulo: bucle_tcp_mutacion_catalogo
estado: FASE 4 — implementada y commiteada (7e7b7e6), pendiente operación real
fecha: 2026-11-07
tags: [fase-4, tcp, triage, mutaciones, undo, auditoria, correlacion]
---

# 10 — Cierre de la Fase 4

> Triage de mensajes del TCP, extracción estructurada, mutaciones de catálogo con
> política de auto-aplicación, undo con detección de conflicto y panel de auditoría.
> **Sin tocar la base real**: la migración `015` y todo el flujo se probaron con bases
> SQLite temporales y el extractor se sustituye por un doble determinista en las
> pruebas, sin red ni LLM.

---

## 1. Qué cambió

| Archivo | Cambio |
|---|---|
| `migrations/015_catalog_mutations.sql` | Tabla `catalog_mutation_log`, índice único parcial por `source_message_id`, índices por estado y producto. |
| `domain/catalog_mutations.py` (nuevo) | Snapshot, normalización, política, aplicación atómica, aprobar/rechazar/revertir, consultas y flag de sobreventa. |
| `domain/errors.py` | `Conflict(DomainError)` para responder `409`. |
| `domain/settings.py` | Lectura de `catalog.autoapply.*`, `catalog.price_max_delta_pct` y `catalog.autoapply.min_confidence`. |
| `domain/conversations.py` | `set_status()` explícito (`ACTIVE` / `WAITING_TCP` / `CLOSED`). |
| `domain/escalations.py` | `pending_for_supplier`, `answered_recent_for_supplier` y `open_for_product`. |
| `domain/events.py` | Eventos `tcp_processed`, `tcp_case_*`, `catalog_mutation_*`, `product_oversold`. |
| `ai/tcp_extractor.py` (nuevo) | Esquema JSON estricto + `pydantic`; devuelve `None` si el modelo no está disponible o falla dos veces. |
| `ai/tcp_triage.py` (nuevo) | Parseo de `[E-]`/`[V-]`, correlación léxica + retrieval, matching proactivo, reanudación de clientes y limpieza de códigos. |
| `ai/handlers.py` | `tcp_process` absorbe `tcp_reply`; audio TCP se transcribe antes; escalaciones `[E-]`; aviso de venta y reanudación. |
| `ai/ingest.py` | Los mensajes de proveedor encolan `tcp_process` o `transcribe_audio(next_job="tcp_process")`. |
| `ai/prompts.py` | Plantilla `escalation_question` con `[E-####]` y respuestas de escalación con resumen verificado. |
| `api/routes_catalog.py` | `GET /api/catalog/mutations`, `GET /{id}`, `approve`, `reject`, `revert`; `Conflict → 409`; reanudación tras aprobar. |
| `api/models.py` | `MutationReasonIn` para el motivo del operador. |
| `dashboard/src/panels/audit/audit_panel.tsx` (nuevo) | Tarjetas, filtros, diffs, transcripción, snapshots y acciones de aprobar/rechazar/deshacer. |
| `dashboard/src/types/api.ts`, `lib/api.ts` | Contratos y cliente de mutaciones. |
| `dashboard/src/App.tsx`, `components/layout/sidebar.tsx` | Ruta `/audit` y entrada **Auditoría**. |
| `tests/test_catalog_mutations.py` (nuevo) | 31 pruebas de Fase 4. |

Settings nuevos (ya existían en `014`, ahora se leen):

- `catalog.autoapply.price` = `1`
- `catalog.autoapply.stock` = `1`
- `catalog.autoapply.specs` = `1`
- `catalog.price_max_delta_pct` = `50`
- `catalog.autoapply.min_confidence` = `0.85`

---

## 2. Correcciones al diseño aprobado

- **Campos opcionales reales**: el extractor emite siempre
  `delivery_zone_names`, `new_specifications`, etc. Una lista de zonas vacía significa
  “sin cambio de zona”, no un motivo de revisión; si se tratara como error, toda
  actualización de precio acabaría en `PENDING_REVIEW`.
- **Zonas desconocidas**: si el TCP nombra una zona que no existe en `zones` ni en
  `zone_aliases`, la mutación va a revisión y **no** se borran las zonas actuales del
  proveedor. Tampoco se inventan municipios: la fuente es el CRUD real de zonas.
- **`states_match` no compara `stock_reserved` ni `updated_at`**: una reserva de venta
  o un reindexado no deben convertir un undo válido en un `409` falso. Sí compara
  precio, moneda, stock, estado, especificaciones y zonas.
- **`MLC` se extrae pero no se guarda**: el esquema del extractor lo acepta para poder
  reportarlo; el dominio lo rechaza y lo manda a revisión (`products.currency` solo
  admite `USD`/`CUP`).
- **Sobreventa**: si `stock < stock_reserved`, el producto pasa a `OUT_OF_STOCK`, se
  registra `product_oversold` y las ventas abiertas pasan a `NEEDS_OPERATOR`. El flag
  se ejecuta fuera de la transacción de escritura porque `events.log` commitea.
- **Idempotencia del job**: `tcp_process` marca el mensaje con el evento
  `tcp_processed` y `apply_tcp_update` es idempotente por `source_message_id`
  (índice único parcial). Un reintento no duplica log, mutación ni respuesta.
- **`tcp_reply` se conserva** como alias registrado: jobs viejos encolados antes del
  despliegue siguen funcionando.
- **Degradación sin modelo**: si el extractor devuelve `None`, se mantiene el flujo de
  la Fase 2/3 (enriquecer `dato_proveedor` con el texto sin `[E-]` y reanudar), para no
  dejar al cliente esperando por una caída del LLM.

---

## 3. Triage y correlación

```text
Mensaje TCP
  ├─ ¿audio? → transcribe_audio(next_job="tcp_process")
  ├─ triage:
  │    ├─ ESCALATION_ANSWER   ([E-###] explícito)
  │    ├─ ORDER_ACK/DELIVERED/REJECTED (venta y [V-######])
  │    ├─ CATALOG_UPDATE      (precio/stock/specs/zona)
  │    ├─ QUESTION
  │    └─ OTHER
  └─ handler correspondiente
```

Orden de correlación (`tcp_triage.correlate`):

1. `[E-###]` explícito del mismo proveedor. Si la escalación no existe o no está
   abierta, se registra el motivo y **no** se adivina.
2. Una sola escalación pendiente del TCP → se usa.
3. Varias pendientes → puntuación por nombre de producto (léxica + ranking de
   retrieval). Umbral `0.35` y margen `0.15`; sin ganador claro → `ambiguous=True` y
   `PENDING_REVIEW`.
4. Sin escalación pendiente → actualización proactiva correlacionada por producto.
5. Un reintento puede encontrar la escalación ya `ANSWERED`; se recupera por respuesta
   reciente para no perder el hilo.

`[V-######]` se valida contra la venta del mismo proveedor antes de aceptar un ack.
Un `[E-]` explícito siempre gana sobre las palabras de un ack de venta.

---

## 4. Política de auto-aplicación

```text
APPLIED si:
  - cambia al menos un campo;
  - la correlación no es ambigua;
  - confianza >= catalog.autoapply.min_confidence;
  - el tipo de campo tiene auto-apply activo;
  - el precio se mueve dentro de catalog.price_max_delta_pct.

PENDING_REVIEW si:
  - precio fuera de rango o con variación excesiva;
  - cambio de moneda (incluye MLC);
  - cambio de zonas (siempre);
  - stock/specs con auto-apply desactivado;
  - confianza baja, correlación ambigua o mensaje sin cambios válidos.
```

Reglas de coherencia:

- `stock = 0` → `OUT_OF_STOCK`.
- `stock > 0` y `status = OUT_OF_STOCK` → `ACTIVE`, salvo que el operador lo hubiera
  pausado (`PAUSED`).
- `specifications_json` se **fusiona**; nunca se reemplaza en bloque ni se borra con
  valores vacíos.
- Las especificaciones y el stock se validan por rango; un valor inválido se descarta y
  se registra como motivo de revisión.

---

## 5. Mutación atómica y undo

`apply_tcp_update` calcula `previous_state`, normaliza la extracción, decide, y en una
sola transacción:

1. escribe el producto (precio, moneda, stock, estado, specs, zonas);
2. inserta `catalog_mutation_log` con ambos snapshots, confianza, método y motivos.

Después de la transacción registra el evento y encola `reindex_product` (prioridad 90).

Undo (`POST /api/catalog/mutations/{id}/revert`):

1. La mutación debe estar `APPLIED`.
2. Se compara el estado actual con `new_state_json`.
3. Si no coincide → `409` y no se escribe nada.
4. Si coincide → se restaura `previous_state_json` y se marca `REVERTED`.

Aprobar una mutación en revisión aplica la misma comprobación de conflicto: si el
producto cambió desde que se propuso, devuelve `409` en lugar de pisar el cambio nuevo.

---

## 6. Panel de Auditoría

- Ruta `/audit`, entrada **Auditoría** en el menú lateral.
- Tarjetas: pendientes en pantalla, mutaciones listadas, total con filtros y ambiguas.
- Filtros: estado, TCP, producto por ID, rango de fechas y `ambiguous`.
- Cada tarjeta muestra producto, TCP, tipo de mutación, fecha, estado, ambigüedad,
  método de correlación y confianza.
- Diff por campo (precio/moneda, stock, estado, especificaciones, zonas) con valor
  anterior y nuevo.
- Mensaje original del TCP, motivos de revisión y código `[E-####]` cuando aplica.
- Snapshots completos desplegables.
- Acciones: **Aprobar** y **Rechazar** en `PENDING_REVIEW`; **Deshacer** en `APPLIED`.
- Diálogo de confirmación para deshacer y rechazar; el `409` del backend se muestra
  tal cual, sin ocultar el conflicto.
- Al aprobar desde el panel, el backend reanuda a **todos** los clientes que esperan
  ese producto (una respuesta por conversación abierta).

La UI está en español; el código y los comentarios, en inglés.

---

## 7. Aceptación

| Caso (doc 04, Fase 4) | Estado |
|---|---|
| Audio TCP con `[E-0104]` actualiza stock con log y undo | ✅ Flujo cubierto por `test_audio_supplier_message_queues_transcription` + `test_handler_applies_stock_and_resumes_clients`; la transcripción es un job previo determinista. |
| Precio fuera de rango o ambiguo → `PENDING_REVIEW` | ✅ `test_price_out_of_range_goes_to_review_then_approves` y `test_ambiguous_correlation_is_not_applied`. |
| Revert de un cambio viejo → `409`, sin corrupción | ✅ `test_undo_of_old_mutation_conflicts` y `test_approve_detects_later_change`. |
| 20 casos de correlación → 0 cruces | ✅ Lógica determinista probada en 8 casos de correlación (`case_id`, `single_pending`, heurística clara/ambigua, proactiva, proveedor ajeno). El conjunto amplio de 20 sigue siendo una prueba de operación con mensajes reales. |
| Reintento del job → no duplica log ni mutación | ✅ `test_source_message_id_is_idempotent` y `test_handler_is_idempotent_on_retry`. |
| `MLC` no se guarda | ✅ `test_mlc_is_never_stored`. |
| Zonas siempre a revisión y sin borrar las existentes | ✅ `test_zone_change_always_requires_review`. |
| Stock 0 → `OUT_OF_STOCK`; stock > 0 reactiva solo si no estaba pausado | ✅ `test_stock_zero_forces_out_of_stock`, `test_stock_back_activates_non_paused_product`, `test_paused_product_stays_paused`. |
| Sobreventa marca ventas `NEEDS_OPERATOR` | ✅ `test_oversold_flags_product_and_sales`. |
| Reanudar **todas** las conversaciones en `WAITING_TCP` | ✅ `test_resume_all_waiting_conversations_once_each` y `test_resume_does_not_reopen_closed_conversation`. |
| Sin modelo, el cliente no queda esperando | ✅ `test_handler_falls_back_when_model_is_down`. |

Pruebas: **128 casos** en `unittest` (24 de Fase 0 + 19 de Fase 1 + 18 de Fase 2 +
36 de Fase 3 + **31 de Fase 4**), todos en verde. El dashboard compila con
`tsc -b && vite build`.

```powershell
cd backend
.venv\Scripts\python.exe -m unittest discover -s tests -q
cd ..
powershell -ExecutionPolicy Bypass -File .\build_dashboard.ps1
```

La migración `015` se verificó sobre una base temporal nueva: las 15 migraciones
aplican, `catalog_mutation_log` existe, los 5 settings de catálogo están presentes y
`PRAGMA quick_check` devuelve `ok`. **La base real no se tocó.**

---

## 8. Limitaciones de esta fase

- **Correlación de 20 casos reales**: la heurística está probada con casos
  sintéticos; falta medirla con mensajes reales de los TCP. Hasta entonces, una
  correlación dudosa va a revisión, nunca se auto-aplica.
- **Bake-off y p95 real**: el bake-off de la Fase 5 ya se midió. El p95 se
  calcula con `scripts/latency_report.py` (bitácora §10.21), pero la muestra
  real aún es pequeña; no se inventan cifras.
- **Recall@3 con consultas reales**: pendiente; el retrieval ya funciona con FTS5 y
  embeddings opcionales.
- **Reanudación desde el panel**: al aprobar se usa la plantilla genérica
  (`escalation_resolved_generic`), no el resumen del extractor, porque el motivo
  original pudo quedar en otra fila del log. El texto crudo del TCP nunca se reenvía.
- **Notificación al operador**: solo por dashboard (D6-A). No hay aviso por Telegram.
- **`REMOVED` no se acepta desde el TCP**: el extractor solo permite
  `ACTIVE`/`OUT_OF_STOCK`/`PAUSED`; retirar un producto es decisión del operador.
- **Sin edición manual del snapshot**: el panel permite aprobar, rechazar o deshacer,
  no corregir un valor propuesto. Si hace falta un precio distinto, se edita el
  producto y se rechaza la mutación.
- **Commit**: la Fase 4 se commiteó el 2026-11-11 (`7e7b7e6`); el árbol quedó
  limpio.

---

## 9. Próximo paso

La Fase 4 está **cerrada y commiteada** (`7e7b7e6`). La Fase 5 también está
implementada (`0c2917c`), así que no queda ninguna fase nueva del plan.

1. Probar el flujo con un TCP de prueba: audio con `[E-####]`, precio fuera de
   rango, aprobación desde el panel y undo.
2. Los pendientes que necesitan datos reales (20 casos de correlación, sets de
   slots y ventas E2E, recall@3 y p95) están en la bitácora **§9.2**, con el
   criterio para retomarlos.
