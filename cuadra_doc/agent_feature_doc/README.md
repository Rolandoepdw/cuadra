---
tipo: indice
proyecto: Cuadra
modulo: agent_feature_doc
estado: DISEÑO APROBADO — fases 0–5 y L3.0–L3.3 en main; L3.4 en el árbol
revisado: 2026-11-19
aprobado: 2026-11-03
tags: [indice, agente-ia, diseno-aprobado, revision, L3]
---

# Documentación del agente autónomo (diseño aprobado)

Esta carpeta contiene el diseño de evolución del agente de Cuadra: memoria, retrieval,
ventas, finanzas, bucle TCP y mutación de catálogo. Las fases 0, 1, 2, 3, 4 y 5 ya están
implementadas; el resto sigue como especificación aprobada que guía la implementación.

> **Empieza por `05_VIABILIDAD_Y_CAMBIOS_REQUERIDOS.md`.**
> Ese documento resume las discordancias con el código real, el veredicto de
> viabilidad, las decisiones aprobadas (D1–D10) y el orden de implementación.

---

## 1. Estado

| Aspecto | Estado |
|---|---|
| Código de producción | **Fases 0, 1, 2, 3, 4 y 5 implementadas:** trazas, turnos persistentes, contexto, guardrails, hechos, resumen incremental, FTS5, embeddings opcionales, RRF, cierre por inactividad, ventas con stock reservado, ledger de comisiones, despacho al TCP, panel de finanzas, triage TCP con mutaciones de catálogo y undo, panel de auditoría y endurecimiento (fugas, divulgación del prompt, cancelaciones y reinicios). |
| Migraciones | `012_observability_and_turns.sql`, `013_memory_retrieval.sql`, `014_sales_and_finance.sql` y `015_catalog_mutations.sql` creadas; se aplican al arrancar (aditivas). |
| Dependencias | `fastembed` añadido a `requirements.txt`. El modelo vendorizado sigue siendo opcional: sin él, el bot usa FTS5 + fuzzy. |
| Modelos | Tres GGUF instalados y medidos con el prompt endurecido (Nemotron-3-Nano-4B default, dos Qwen3-4B); ver `12_CIERRE_FASE_5.md` §1.3. |
| Base de datos real | Sin cambios hasta el próximo arranque. |
| Commit | Fases 0–5 commiteadas en `main`; ver `git log` para los hashes. La Fase 5 cierra el plan de 6 fases. |
| Aprobación | Aprobada por el dueño el 2026-11-03. |

La propuesta original llegó marcada como `DISENO_APROBADO`; tras la revisión pasó a
`PROPUESTA REVISADA` porque contenía supuestos que no coinciden con el esquema real
(estados de venta, columnas inexistentes, migraciones duplicadas, modelo no instalado
y audio TCP que hoy no se transcribe).

El dueño revisó la propuesta corregida y aprobó D1–D10 el **2026-11-03**. Desde
entonces esta carpeta es el diseño de referencia para implementar, empezando por la
Fase 0 (medición). Las fases 0–5 ya están implementadas y commiteadas.

---

## 2. Orden de lectura

| Orden | Documento | Qué responde |
|---|---|---|
| 1 | `05_VIABILIDAD_Y_CAMBIOS_REQUERIDOS.md` | ¿Es viable? ¿Qué estaba mal? ¿Qué se decidió y en qué orden se implementa? |
| 2 | `00_ARQUITECTURA_Y_MODELO_AUTONOMO.md` | Arquitectura, modos de autonomía, estados y guardrails. |
| 3 | `01_MEMORIA_RETRIEVAL_Y_CATALOGO.md` | Turnos, memoria, FTS5, embeddings y presupuesto de contexto. |
| 4 | `02_MAQUINA_ESTADOS_VENTAS_Y_FINANZAS.md` | Venta, stock reservado, ledger, API y dashboard. |
| 5 | `03_BUCLE_TCP_MUTACION_CATALOGO_Y_ROLLBACK.md` | Triage TCP, audio, mutaciones y undo. |
| 6 | `04_PLAN_IMPLEMENTACION_Y_PROMPTS_MAESTROS.md` | Fases, contratos, prompts y pruebas. |
| 7 | `06_LINEA_BASE_FASE_0.md` | Métricas base del modelo actual y fallos detectados. |
| 8 | `07_CIERRE_FASE_1.md` | Turnos, idempotencia, contexto y guardrails: qué se implementó y qué falta medir. |
| 9 | `08_CIERRE_FASE_2.md` | Hechos, resumen, FTS5, embeddings opcionales, RRF y cierre por inactividad. |
| 10 | `09_CIERRE_FASE_3.md` | Ventas, stock reservado, ledger de comisiones, despacho al TCP y panel de finanzas. |
| 11 | `10_CIERRE_FASE_4.md` | Triage TCP, mutaciones de catálogo, undo con conflicto y panel de auditoría. |
| 12 | `11_RUNBOOK_OPERACION.md` | Cómo aprobar, cancelar, revertir, reindexar y restaurar en operación real. |
| 13 | `12_CIERRE_FASE_5.md` | Endurecimiento, replay del golden set, recall@3 y métricas contra los criterios de aceptación. |
| 14 | `13_BITACORA_OPERACION_REAL.md` | Pruebas end-to-end en vivo con Telegram y la BD de prueba: plan, resultados y hallazgos. |
| 15 | `14_VISION_Y_CAPACIDADES_DEL_AGENTE.md` | Visión L3: contrato de capacidades, orquestador, TurnFrame, grounding, modos y decisiones D11–D22. |
| 16 | `15_PLAN_FASE_L3.md` | Plan ejecutable de L3: cortes L3.0–L3.7, migración multi-categoría, ranking, pruebas y criterios de encendido. |
| — | `../10_contexto_agente_ia.md` | Brief original que alimentó la propuesta. |

---

## 3. Leyenda

| Marca | Significado |
|---|---|
| **CORREGIDO** | Ya ajustado en los documentos tras comparar con el código real. |
| **DECISIÓN** | Punto que requería respuesta del dueño (`D1`–`D10`). Resueltas en `05`, sección 4. |
| **BLOQUEANTE** | Si se implementa tal cual, falla o corrompe datos. |
| **ALTA** | Funciona mal o genera deuda peligrosa. |
| **MEDIA** | Fricción o inconsistencia, no impide avanzar. |
| **DIFERIDO** | Se reconoce, pero no se implementa en esta fase. |

---

## 4. Decisiones aprobadas

Están detalladas en `05`, sección 4. Resumen:

| ID | Tema | Decisión aprobada |
|---|---|---|
| D1 | Modo de autonomía inicial | `assisted` al inicio; **desde D23 (2026-11-19) el default es `autonomous`** |
| D2 | Mutación automática de precio | Auto con límites (±50 %, confianza ≥ 0.85) |
| D3 | Zonas | CRUD `zones`; cualquier zona activa es válida |
| D4 | Moneda de comisión | Cruzada; `supplier_payout` nulo y “requiere conversión” |
| D5 | Venta multi-producto | Diferida; una venta = un producto |
| D6 | Notificación al operador | Solo dashboard |
| D7 | Trazas y privacidad | Redactadas (`redacted=1`, 30 días) |
| D8 | Modelo | Bake-off en Fase 0 con los modelos del dueño |
| D9 | Prompts | Mantener `prompts.py` |
| D10 | Tests | Mantener `unittest` |
| D23 | Defaults de operación | `sales.mode=autonomous`, `sales.followup.mode=shadow`; sin setup manual tras borrar la BD |

---

## 5. Reglas que no cambian

- Windows, offline, $0, 4 GB de VRAM, 32 GB de RAM, SQLite, un proceso Python.
- El operador cierra el pago y cobra la comisión.
- El LLM nunca escribe SQL ni recibe datos internos (comisión, margen, costo).
- El bot nunca inventa precio, stock, garantía ni zona.
- Los grupos de difusión no se borran: se desactivan.
- No se hace commit sin pedido explícito del dueño.

---

## 6. Próximo paso

La **Fase 0** está implementada y medida con el modelo actual (ver `06`):

1. Migración `012`: `llm_calls` con trazas redactadas. ✅
2. Golden set sintético de 55 turnos NLU + 12 de redacción. ✅
3. Medición base: 81.8 % de intención, 0 fugas, p95 3.6 s. ✅
4. Bake-off: falta correr el mismo evaluador con cada modelo que instale el dueño.

La **Fase 1** está implementada, probada y commiteada (ver `07`): ráfaga = turno =
respuesta, deduplicación por `external_id`, recuperación tras caída, presupuesto de
contexto y guardrails de salida. Queda pendiente medir el p95 real cuando el dueño
levante el modelo.

La **Fase 2** está implementada y probada (ver `08`): hechos vigentes, perfil de
cliente, resumen incremental por id de mensaje, FTS5 con BM25, embeddings opcionales
con FastEmbed, fusión RRF, fallback fuzzy y cierre de conversaciones inactivas. El
modelo está instalado y vendorizado en `backend/models/fastembed/`; el arranque no
descarga y la inferencia offline funciona. Falta medir recall@3 con consultas reales;
si el modelo falta, el bot sigue respondiendo con FTS5 + fuzzy.

La **Fase 3** está implementada, probada y commiteada (`05c4d37`, ver `09`): venta de
punta a punta, stock reservado sin descontar inventario hasta la entrega, ledger de
comisiones por moneda, despacho al TCP con acks `RECIBIDO`/`ENTREGADO`/`AGOTADO`,
recuperación de despachos tras un reinicio y panel **Finanzas**. Queda probar una venta
real con un TCP de prueba.

La **Fase 4** está implementada, probada y commiteada (`7e7b7e6`, ver `10`): triage de
mensajes TCP (texto y audio), correlación por `[E-###]`/`[V-######]` y heurística con
revisión ante ambigüedad, extracción estructurada, mutaciones atómicas de precio/stock/
specs/zonas con política de auto-aplicación, undo con detección de conflicto (`409`) y
panel **Auditoría**. Falta medir la correlación con mensajes reales.

La **Fase 5** está implementada (ver `12`): replay completo del golden set contra el
modelo local, comparación A/B de prompts con el mismo set, `recall@3` offline sobre las
consultas etiquetadas, 118 ataques de fuga/divulgación bloqueados, pruebas de
cancelación y de reinicio a mitad de turno, y runbook de operación (`11`). Las métricas
que dependen de operación real (slots, ventas end-to-end, correlación TCP real) quedan
documentadas como desviación con plan.

La operación real no se toca: cualquier cambio de comportamiento se habilita con
settings (`sales.mode`, `catalog.autoapply.*`), no reescribiendo el flujo.

Con la Fase 5 se completa el plan de **6 fases (0–5)**. Lo que queda son tareas de
operación real, no fases nuevas: correlación con mensajes TCP reales, sets de slots y
de ventas end-to-end, y medición de recall@3/p95 con muestra suficiente. Están
detalladas con su criterio de cierre en la bitácora **§9.2** (`13`).

---

## 7. Fase L3 — capa de lectura del agente

La prueba #8 («accesorios para laptop» incluyó la laptop) no fue un bug de catálogo:
fue el patrón de **una afirmación factual sin una lectura del turno que la
respaldara**. El mismo patrón puede repetirse en stock, zonas, fechas, estado de
pedido, escalaciones y memoria.

L3 lo corrige de forma estructural, no con otra cláusula:

- **Orquestador explícito** (`ai/orchestrator.py`): secuencia el turno y garantiza
  los invariantes.
- **TurnFrame** (`ai/turn/frame.py`): conserva producto en juego, último listado,
  oferta pendiente, venta activa, hechos y perfil. Resuelve «esos», «el segundo» y
  «el de antes».
- **Contrato de capacidad** (`ai/tools/base.py`): `ToolSpec`, `ToolResult`,
  errores estables, modos `off`/`shadow`/`on`, cache por turno.
- **Tools de lectura** por dominio: catálogo, ventas, zonas, memoria, escalaciones
  y conocimiento. Las escrituras siguen solo en la máquina de estados.
- **Grounding** (`ai/grounding.py`): toda afirmación de precio, stock, zona, fecha,
  estado de pedido, existencia o specs necesita evidencia del turno.
- **Calidad antes que latencia:** sin tope de iteraciones ni de tools; solo un
  watchdog de cuelgue (`AGENT_TURN_WATCHDOG_S`, default 180 s).

El plan está en `15_PLAN_FASE_L3.md` y la visión/decisiones en
`14_VISION_Y_CAPACIDADES_DEL_AGENTE.md`. **L3.0 no cambia el comportamiento del
bot**: extrae el orquestador y fija el contrato. Las capacidades nuevas empiezan en
L3.1 (catálogo) y siguen ventas/zonas, memoria, escalaciones y conocimiento.

**L3.1 ya está implementada y mergeada en `main`** (`8b52c14`): catálogo
multi-categoría, ranking determinista, referencias («esos», «el segundo», «el de
antes») y grounding de negaciones en `shadow`.

**L3.2 ya está implementada y mergeada en `main`** (`a304081`): tools de venta,
oferta, zona y fecha (`sales.status`, `sales.history`, `offers.pending`,
`zones.check`, `zones.list`, `logistics.eta`) y grounding de estado, cobertura y
fecha.

**L3.3 ya está implementada y mergeada en `main`** (`71c2a8e`): `memory.facts`,
`memory.profile` y `memory.summary`, grounding de hechos («ya me dijiste» / «no
me has dicho») y redacción de PII en las trazas de tools.

**L3.4 está implementada en el árbol de trabajo**: `escalations.status` y
`escalations.pending`, y grounding de «ya consulté», «el proveedor confirmó» y
«sigo esperando». La suite queda en **614 tests**.
