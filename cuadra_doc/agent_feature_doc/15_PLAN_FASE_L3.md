---
tipo: plan_implementacion
proyecto: Cuadra
modulo: agente_ia
estado: L3.0 y L3.1 implementados y mergeados en main — sin push
revisado: 2026-11-19
tags: [plan, L3, orquestador, tools, catalogo, grounding, evaluacion]
---

# 15 — Plan de la fase L3 (capa de lectura del agente)

> **Documento de ejecución.** La visión y las decisiones están en
> `14_VISION_Y_CAPACIDADES_DEL_AGENTE.md`. Este documento dice **qué se hace, en
> qué orden, con qué pruebas y cuándo se considera terminado**.
>
> **Regla de oro:** L3.0 no cambia el comportamiento del bot. Extrae el
> orquestador y fija el contrato. Las capacidades nuevas empiezan en L3.1.

---

## 0. Objetivo y criterio de éxito

**Objetivo:** que toda afirmación factual del agente esté respaldada por una
lectura del turno, sin reescribir el bot cada vez que aparece un dominio nuevo.

**Criterio de éxito de L3:**

1. La prueba #8 y sus variantes quedan cubiertas por tests, no por una cláusula.
2. Un listado de categoría devuelve hasta 10 coincidencias reales, sin duplicados,
   incluidos agotados, y entiende «esos», «el segundo» y «el de antes».
3. Una negación («no hay X») solo se emite si la lectura del turno la sostiene.
4. Las lecturas de venta, zona y fecha también tienen evidencia.
5. El modelo no puede ejecutar una escritura.
6. La suite completa y `check_all.ps1 -SkipBuild` siguen verdes.
7. El bot no envía dos respuestas por turno, ni siquiera con watchdog o reintento.
8. Todo se puede apagar con settings (`agent.tools.mode`, `agent.grounding.mode`).

---

## 1. Reglas de ejecución

| Regla | Detalle |
|---|---|
| Idioma | Documentación y UI en español; código, nombres y comentarios en inglés. |
| Tests | `unittest`, sin pytest. |
| Commits | Solo con pedido explícito. Sin push. Merge a `main` solo si todo está verde y el dueño lo decide en el momento. |
| Ramas | Una rama por corte (`feat/l3-0-orchestrator`, `feat/l3-1-catalog`, …). |
| Secretos | No se exponen ni se persisten; trazas redactadas. |
| Compatibilidad | `pipeline.handle_turn` sigue siendo la entrada pública. Los tests que importan funciones privadas siguen funcionando con alias. |
| Comportamiento | L3.0 es extracción sin cambio de comportamiento. Si un test cambia, se investiga antes de tocar el test. |
| Datos | Migraciones aditivas; backfill; sin borrar columnas. |
| Operación | Todo cambio de comportamiento se enciende con settings, no reescribiendo el flujo. |

---

## 2. Arquitectura objetivo

```
backend/cuadra/ai/
  orchestrator.py          # run_turn: fases, watchdog, una respuesta por turno
  turn/
    __init__.py
    frame.py               # TurnFrame: contexto, historial, foco, listado, oferta, venta
    references.py          # "esos", "el segundo", "el de antes"
    policy.py              # TurnPlan + decisión determinista por dominio
    composer.py            # ContextBuilder + LLM + guardrails + evidencia
    render.py              # fichas, tablas, plantillas (movidas de pipeline)
    sales_flow.py          # pasos de venta (movidos de pipeline)
  tools/
    __init__.py
    base.py                # ToolSpec, ToolResult, ToolError, registry, cache
    catalog.py
    sales.py
    zones.py
    memory.py
    escalations.py
    knowledge.py
  grounding.py             # validación de afirmaciones
  pipeline.py              # fachada + alias de compatibilidad
```

Se mantienen sin mover: `context.py`, `guardrails.py`, `prompts.py`, `offers.py`,
`memory.py`, `llm.py`, `queue.py`, `handlers.py`, `tracing.py` y `domain/*`.

### 2.1 Mapa de extracción de `pipeline.py`

| Bloque actual | Destino | Cambio de comportamiento |
|---|---|---|
| `handle_turn`, `_run_turn` | `orchestrator.py` | ninguno en L3.0 |
| `_decide`, `_stage_for`, `_maybe_handle_sale` | `turn/policy.py` y `turn/sales_flow.py` | ninguno en L3.0 |
| `_compose_open`, `_build_base_messages`, `_safe_template` | `turn/composer.py` | ninguno en L3.0 |
| `_render_*`, `_format_*`, `_table_cell` | `turn/render.py` | ninguno |
| regexes y helpers de routing (`_catalog_request`, `_banal_kind`, `_is_greeting`, …) | `turn/policy.py` o `turn/references.py` | ninguno |
| `_resolve_product`, `_retrieve_candidates` | `turn/references.py` | se amplía con `last_listing` en L3.1 |
| `_update_context` | `turn/frame.py` (`commit`) | ninguno; se añaden claves aditivas |
| `handle_incoming`, `maybe_summarize` | `pipeline.py` (fachada) | ninguno |

`pipeline.py` conserva alias:

```python
# Compatibilidad con tests y llamadas existentes.
from cuadra.ai.orchestrator import handle_turn, handle_incoming, maybe_summarize
from cuadra.ai.turn.references import _resolve_product, _retrieve_candidates
from cuadra.ai.turn.render import _render_product_card, _format_product_table
from cuadra.ai.turn.policy import _stage_for, _maybe_handle_sale
# ...
```

Los alias se van retirando cuando los tests migren, pero no son una deuda
peligrosa: son una capa de compatibilidad documentada.

---

## 3. Cortes

| Corte | Nombre | Depende de | Tamaño | Enciende algo nuevo |
|---|---|---|---|---|
| **L3.0** | Orquestador, TurnFrame y contrato | — | L | No |
| **L3.1** | Catálogo como herramienta | L3.0 | L | Sí (`agent.tools.catalog.mode`) |
| **L3.2** | Ventas, ofertas, zonas y fechas | L3.1 | M | Sí (`agent.tools.sales.mode`) |
| **L3.3** | Memoria y cliente | L3.1 | M | Sí (`agent.tools.memory.mode`) |
| **L3.4** | Escalaciones y TCP | L3.2 | S | Sí (`agent.tools.escalations.mode`) |
| **L3.5** | Conocimiento y políticas (RAG) | L3.3 | M | Opcional |
| **L3.6** | Tool calling del modelo en `shadow` → `on` | L3.1–L3.4 | L | Sí (`agent.tools.mode`) |
| **L3.7** | Endurecimiento, evaluación y runbook | Todos | M | No |

L3.5 es opcional: si no hay corpus curado, se documenta y se difiere. No bloquea
L3.6.

---

## 4. L3.0 — Orquestador, TurnFrame y contrato

### 4.1 Objetivo

Sacar el ciclo de vida del turno de `pipeline.py`, fijar el contrato de capacidad
y dejar el marco del turno listo para el catálogo. **Sin cambiar respuestas.**

### 4.2 Entregables

1. `ai/orchestrator.py`
   - `async def run_turn(conn, conversation, turn) -> dict` con las fases del
     documento 14 §6.3.
   - `handle_turn` se mueve aquí y `pipeline.handle_turn` queda como alias.
   - `handle_incoming` y `maybe_summarize` se mantienen en la fachada.
   - Traza raíz `pipeline.handle_turn` (nombre actual, para no romper el
     dashboard) + trazas hijas `orchestrator.assemble`, `orchestrator.plan`,
     `orchestrator.read`, `orchestrator.decide`, `orchestrator.compose`.
   - Watchdog con `asyncio.wait_for` y cancelación limpia.

2. `ai/turn/frame.py`
   - `TurnFrame` (dataclass) y `build_frame(...)`.
   - `commit(frame)` envuelve `_update_context` y persiste las claves aditivas.
   - `last_listing` y `pending_offer` con payload se escriben ya en L3.0 (aunque
     el catálogo todavía no las use), para no cambiar el formato dos veces.

3. `ai/turn/references.py`
   - `resolve_reference(text, frame)` con ordinales, demostrativos y foco.
   - En L3.0 se usa solo para logging/telemetría; la decisión sigue igual. Se
     enciende en L3.1.

4. `ai/turn/policy.py`
   - `TurnPlan` (dataclass): `reads: list[ToolCall]`, `decision: str`,
     `reason: str`, `fallback: str | None`.
   - `plan_turn(frame, intent) -> TurnPlan` con la misma lógica de `_decide`,
     pero devolviendo un plan en vez de ejecutar todo.
   - `apply_plan(frame, plan) -> Outcome` con la decisión determinista.
   - En L3.0, `plan_turn` puede devolver un plan trivial (`reads=[]`,
     `decision="legacy"`) y `apply_plan` llamar al código movido. Lo importante es
     que el orquestador ya tenga el punto de extensión.

5. `ai/turn/composer.py`
   - `_compose_open`, `_build_base_messages`, `_safe_template` movidos.
   - `compose_reply(frame, outcome, evidence) -> (reply, generator)`.

6. `ai/turn/render.py` y `ai/turn/sales_flow.py`
   - Movimiento mecánico de renderers y pasos de venta.

7. `ai/tools/base.py`
   - `ToolSpec`, `ToolResult`, `ToolError`, `ToolCall`, `Registry`.
   - `registry.register(spec, fn)`, `registry.run(name, args, frame)`.
   - Cache por turno en `frame.cache`.
   - `Registry.allowed(mode)` filtra por modo y permiso.
   - En L3.0 se registran tres lecturas existentes como prueba:
     `catalog.search`, `catalog.get`, `catalog.list_category`.

8. `ai/grounding.py`
   - `Claim`, `Violation`, `validate_claims(reply, frame)`.
   - En L3.0 solo implementa precio/specs (delegando en `guardrails`) y deja el
     esqueleto de negaciones. `agent.grounding.mode=shadow` por defecto.

9. `config.py`
   - `AGENT_TURN_WATCHDOG_S = 180` (`0` desactiva).
   - `AGENT_TOOLS_MODE = "off"`.
   - `AGENT_TOOLS_CACHE_TTL_S = 0`.
   - `AGENT_GROUNDING_MODE = "shadow"`.
   - `AGENT_TOOL_TIMEOUT_S = 5`.

10. Tests nuevos
    - `tests/test_orchestrator.py`: fases, una respuesta por turno, watchdog.
    - `tests/test_frame.py`: `last_listing`, `pending_offer`, commit.
    - `tests/test_references.py`: ordinales, demostrativos, ambigüedad.
    - `tests/test_tools_contract.py`: `ToolSpec`, `ToolResult`, errores, cache,
      registry cerrado.
    - `tests/test_grounding.py`: precio/specs en `shadow`; negación sin evidencia
      se registra y no cambia la respuesta.

### 4.3 Criterios de aceptación

- [x] La suite completa sigue verde (527 tests).
- [x] `check_all.ps1 -SkipBuild` verde.
- [x] Replay de 20 conversaciones: respuestas y contexto idénticos contra `main`
      (determinista: BD temporal, NLU y compositor scripteados, sin Telegram). Un
      replay con tráfico real sigue siendo opcional antes de encender L3.1.
- [x] `pipeline.handle_turn` funciona igual; el dashboard sigue viendo la traza
      raíz.
- [x] Un test simula un cuelgue y verifica: watchdog, plantilla segura, una sola
      respuesta, sin reintento.
- [x] Los tests que importan funciones privadas de `pipeline` siguen pasando.
- [x] `agent.tools.mode=off` no ejecuta ninguna tool propuesta por el modelo.

### 4.4 Riesgos y mitigación

| Riesgo | Mitigación |
|---|---|
| Cambiar comportamiento al mover código | Cortes pequeños, suite después de cada uno, replay de conversaciones. |
| Doble traza / métricas infladas | Mantener `pipeline.handle_turn` como raíz; las fases son hijas. |
| Tests acoplados a privates | Alias en `pipeline.py`; migración gradual. |
| Watchdog que corta turnos válidos | Default alto (180 s) y configurable; se calibra con datos reales. |
| Duplicar respuesta al cancelar | Marcar el turno como terminado y no reintentar. |

### 4.5 Estado de implementación (2026-11-19)

Implementado en la rama `feat/l3-0-orchestrator`, sin commit ni push todavía.

**Archivos nuevos:**

| Archivo | Contenido |
|---|---|
| `ai/orchestrator.py` | `handle_turn`, `run_turn`, `_run_with_watchdog`, `_timeout_result`; trazas hijas `orchestrator.assemble`, `orchestrator.interpret`, `orchestrator.resolve`, `orchestrator.plan`, `orchestrator.decide`, `orchestrator.compose`, `orchestrator.persist`; registro de las tools de catálogo al importar. |
| `ai/turn/__init__.py` | Paquete del ciclo de turno. |
| `ai/turn/constants.py` | Regexes y constantes compartidas, movidas de `pipeline.py`. |
| `ai/turn/frame.py` | `TurnFrame`, `Listing`, `build_frame`, `commit`, `_update_context`. |
| `ai/turn/references.py` | `Reference`, `resolve_reference` (ordinales, demostrativos, foco, ambigüedad) + `_resolve_product`. |
| `ai/turn/policy.py` | `TurnPlan`, `plan_turn` trivial y la decisión determinista movida. |
| `ai/turn/composer.py` | `_build_base_messages`, `_fast_template`, `_compose_open`. |
| `ai/turn/render.py` | Fichas, tablas y helpers de specs. |
| `ai/turn/guards.py` | `_guard_context`, `_guard_product`, `_safe_template`, `_log_guardrail`. |
| `ai/turn/sales_flow.py` | Pasos de venta, ofertas, zonas y post-venta. |
| `ai/tools/__init__.py`, `ai/tools/base.py` | `ToolSpec`, `ToolResult`, `ToolError`, `ToolCall`, `Registry` cerrado, cache por turno, `allowed(mode)`. |
| `ai/tools/catalog.py` | `catalog.search`, `catalog.get`, `catalog.list_category` como prueba del contrato. |
| `ai/grounding.py` | `Claim`, `Violation`, `GroundingReport`, `validate_claims`; precio/specs delegan en `guardrails`; negaciones solo con evidencia del turno. |

`ai/pipeline.py` queda como fachada: `handle_incoming`, `maybe_summarize` y
alias de compatibilidad (`handle_turn`, `_run_turn` y todas las funciones
privadas que usan los tests).

**Cambios de soporte:**

- `config.py`: `AGENT_TURN_WATCHDOG_S=180`, `AGENT_TOOLS_MODE=off`,
  `AGENT_TOOLS_CACHE_TTL_S=0`, `AGENT_GROUNDING_MODE=shadow`,
  `AGENT_TOOL_TIMEOUT_S=5`.
- `prompts.py`: plantilla `turn_timeout` («Ahora mismo no puedo confirmarte ese
  dato. Dame un momento y te respondo.»), sin placeholders.
- Watchdog: al dispararse registra el evento `turn_timeout`, devuelve la
  plantilla segura, marca `outcome="timeout"` y no reintenta. `0` lo desactiva.
- Grounding en `shadow`: registra `grounding_violation` en `events` y no cambia
  la respuesta. El chequeo de negaciones solo corre cuando el frame trae
  evidencia de tools (en L3.0 todavía no hay lecturas en el flujo).
- Tests nuevos: `test_orchestrator.py`, `test_frame.py`, `test_references.py`,
  `test_tools_contract.py`, `test_grounding.py` (46 tests).
- `test_turns.py`: el patch de `_retrieve_candidates` apunta ahora a
  `cuadra.ai.turn.references`, su módulo real.
- `test_turn_outcomes.py`: el fallo simulado se parchea en
  `cuadra.ai.orchestrator.run_turn`.

**Desviaciones conscientes respecto de §4.2:**

- No se añadió `apply_plan(frame, plan)`: en L3.0 el plan es trivial y la
  decisión sigue en `_decide`, que el orquestador ya traza como
  `orchestrator.decide`. `apply_plan` nace en L3.1, cuando el plan trae lecturas.
- Tampoco se añadió `compose_reply(frame, outcome, evidence)`: la composición
  sigue dentro de la ruta determinista. La evidencia entra en L3.1, junto con
  las tools de catálogo.
- La fase `orchestrator.read` no se emite todavía: no hay lecturas que trazar.
  `compose` y `persist` sí se emiten, con la duración ya medida por el turno.
- Los eventos usan la fuente `system` (no `agent`, que no está en `SOURCES`).

**Pendiente para cerrar L3.0:**

- Revisión del dueño y decisión de commit/merge.
- Replay opcional con tráfico real antes de encender L3.1.

**Replay determinista (2026-11-19):** `scripts/replay_turns.py` ejecuta 20
conversaciones (30 turnos) con BD temporal, NLU y compositor scripteados, y sin
enviar nada por Telegram. El reporte de la rama y el de `main` (`49d8bbe`) son
idénticos salvo `last_greeting_at`, que es reloj de pared: mismas respuestas y
mismo `context_json` en los 20 casos.

---

## 5. L3.1 — Catálogo como herramienta

### 5.1 Objetivo

Que el catálogo sea una lectura real, multi-categoría y con procedencia. Es el
corte que cierra la prueba #8 de forma estructural.

### 5.2 Migración `024_product_categories.sql`

```sql
-- Un producto puede estar en varias categorías; `products.category_id` sigue
-- siendo la primaria para compatibilidad y para el panel.
CREATE TABLE IF NOT EXISTS product_categories (
    product_id  INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    is_primary  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (product_id, category_id)
);

CREATE INDEX IF NOT EXISTS idx_product_categories_category
    ON product_categories(category_id);

-- Backfill: la categoría actual pasa a ser la primaria.
INSERT OR IGNORE INTO product_categories (product_id, category_id, is_primary)
SELECT id, category_id, 1
FROM products
WHERE category_id IS NOT NULL;

-- Recrear los triggers de FTS para que `category` sea la concatenación de todas
-- las categorías del producto, no solo la primaria.
DROP TRIGGER IF EXISTS trg_products_fts_ai;
DROP TRIGGER IF EXISTS trg_products_fts_au;
-- ... CREATE TRIGGER con group_concat sobre product_categories ...
-- ... triggers nuevos en product_categories para reconstruir la fila FTS ...

-- Reindexar una vez.
DELETE FROM products_fts;
INSERT INTO products_fts (product_id, name, description, category, specifications)
SELECT p.id, p.name, COALESCE(p.description, ''),
       COALESCE((SELECT group_concat(c.name, ' ')
                 FROM product_categories pc
                 JOIN categories c ON c.id = pc.category_id
                 WHERE pc.product_id = p.id), ''),
       COALESCE(p.specifications_json, '{}')
FROM products p;
```

Notas:

- Migración aditiva; no se borra `products.category_id`.
- `catalog.create_product` y `update_product` escriben la tabla puente. Si el
  payload trae `category_ids`, se reemplaza el conjunto; si trae `category_id`,
  se actualiza la primaria y se asegura la fila.
- `catalog.list_products`, `get_product` y `llm_product_json` incluyen
  `categories: list[str]`.
- Panel: selector múltiple de categorías; la primaria se marca aparte.
- Rollback: `DROP TABLE product_categories;` y restaurar los triggers de 013.
  Los datos de producto no se tocan.

### 5.3 Ranking determinista

`search_catalog` no usa el LLM para rankear. Algoritmo:

1. **Normalizar** la consulta: fold, sin acentos, singular, sin stopwords
   (`de`, `para`, `con`, `y`, `en`, `la`, `el`, `los`, `las`, `un`, `una`,
   `que`, `tienes`, `hay`, `productos`, `dame`, `lista`, `listado`, `busco`,
   `quiero`, `necesito`, `muéstrame`).
2. **Puntuar categorías** por palabras de la consulta cubiertas por el nombre de
   la categoría. Preferir la más específica (la que cubre más palabras y tiene
   menos productos). Empate: id ascendente.
3. **Recoger productos** de la categoría ganadora y de las categorías hermanas
   que compartan al menos una palabra, usando `product_categories`.
4. **Puntuar productos** con:
   - relevancia contra la consulta y la categoría (0.60),
   - solapamiento de tokens del nombre (0.25),
   - frase exacta en el nombre (0.10),
   - disponibilidad (0.05): **solo desempata**, no mete productos que no
     coinciden.
5. **Deduplicar** por `product_id`; la categoría primaria manda.
6. **Cortar por relevancia, no por número.** Solo entran productos que
   pertenecen a la categoría ganadora o cuyo nombre cubre los calificadores de la
   consulta. Si hay 4, se muestran 4; si hay 6, se muestran 6. No se rellena con
   productos de otra categoría ni con coincidencias débiles.
7. **Ordenar** por relevancia descendente. Empate: primero stock > 0; después
   categoría primaria; después id ascendente.
8. **Límite:** hasta 10 coincidencias. Si hay menos, se muestran todas. No hay
   mínimo de 5.
9. **Agotados:** se incluyen si coinciden y queda capacidad, con
   `availability="out_of_stock"`. Si ya hay 10 mejores coincidencias, la agotada
   no entra. Nunca se oculta un agotado que sea la única coincidencia.
10. **Vacío:** devolver `empty_reason`:
    - `no_category`: ninguna categoría coincide;
    - `category_empty`: la categoría existe pero no tiene productos;
    - `no_match`: no hubo coincidencia léxica.
11. **Nunca** inventar o mezclar un producto para llenar la tabla. «Comida» no
    puede devolver una laptop aunque sea lo más parecido que haya.

### 5.4 Tools de catálogo

```python
class SearchCatalogInput(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=10, ge=1, le=10)
    include_out_of_stock: bool = True
    category_hint: str | None = None


class CatalogProduct(BaseModel):
    id: int
    name: str
    categories: list[str]
    price: float
    currency: str
    stock: int
    status: str
    availability: Literal["in_stock", "out_of_stock", "paused"]
    highlights: str


class SearchCatalogResult(BaseModel):
    query: str
    products: list[CatalogProduct]
    matched_categories: list[str]
    empty_reason: str | None
```

Tools:

| Tool | Entrada | Salida | Uso |
|---|---|---|---|
| `catalog.search` | `SearchCatalogInput` | `SearchCatalogResult` | «qué tienes de X», «busco X» |
| `catalog.get` | `{product_id}` | `CatalogProduct` + specs + zonas | ficha, detalle, comparación |
| `catalog.list_category` | `{category, limit=10}` | `SearchCatalogResult` | «lista completa», «todos» |
| `catalog.check_stock` | `{product_id, quantity=1}` | `{available, stock, status}` | «¿hay?», «¿me alcanza?» |

`catalog.get` devuelve `llm_product_json` (sin costo, margen ni comisión) más
`categories` y `zones` efectivas. Las zonas se leen con `catalog.effective_delivery_zones`,
pero la tool de cobertura es de L3.2.

### 5.5 Referencias y listado

- `last_listing` se guarda al responder con tabla o lista.
- `pending_offer` guarda `product_ids` y `query` para alternativas/hermanos.
- `resolve_reference` se enciende en la política:
  - «el segundo» → `last_listing.product_ids[1]`;
  - «esos» → todos los del último listado;
  - «el de antes» → `focus_product` o el único del listado;
  - ambigüedad → preguntar.
- Si el cliente pide un producto concreto después de un listado, se resuelve
  contra el listado antes de buscar en todo el catálogo.

### 5.6 Grounding de negaciones

- `agent.grounding.mode=shadow` en L3.1; se registra la violación.
- `agent.grounding.mode=on` cuando el set de negaciones esté completo.
- Reglas del documento 14 §8.2.
- El compositor recibe el bloque `EVIDENCIA DE ESTE TURNO`.
- Si la evidencia es vacía, la plantilla dice «No encontré X en el catálogo»
  (no «no hay X» a secas) y ofrece alternativas.

### 5.7 Tests

- `tests/test_catalog_tools.py`
  - «accesorios para laptop» lista Mouse/Audífonos/Teclado y **no** la laptop.
  - Producto en dos categorías aparece una sola vez.
  - 4 productos → 4; 6 productos → 6; 12 productos → 10; sin relleno.
  - Agotado incluido y marcado.
  - Categoría inexistente → `empty_reason="no_category"`.
  - Categoría vacía → `empty_reason="category_empty"`.
- `tests/test_references.py`
  - «la segunda», «esos», «el de antes», «el mismo».
  - Ambigüedad → pregunta.
- `tests/test_grounding.py`
  - «no hay laptops» con 3 laptops → violación.
  - «no hay laptops» con `empty_reason` → válida.
  - Sin tool → violación en `on`, log en `shadow`.
- `tests/test_turns.py` (ampliar)
  - Prueba #8 original y variantes.
  - Petición de catálogo en medio de una venta.
  - Petición de catálogo después de una venta `DONE`.
  - «¿qué procesador trae la laptop?» después de un listado de accesorios.

### 5.8 Criterios de aceptación

- [x] La prueba #8 y sus variantes pasan.
- [x] Hasta 10 coincidencias reales, sin duplicados, agotados incluidos y sin relleno.
- [x] «Esos», «el segundo» y «el de antes» resuelven contra el último listado.
- [x] «No hay X» solo con evidencia; 0 falsos negativos en el set.
- [x] Sin regresiones en la suite (553 tests) y replay sintético de 20 conversaciones.
- [x] `agent.tools.catalog.mode=off` mantiene el comportamiento actual.
- [x] La migración se aplica en una BD de prueba y la BD local; el rollback está documentado.

### 5.9 Estado de implementación (2026-11-19)

Implementado en `feat/l3-0-orchestrator` y mergeado a `main` (`8b52c14`),
sin push todavía.

**Datos y dominio:**

- `migrations/024_product_categories.sql`: tabla puente, backfill de la
  categoría primaria, triggers de sincronización `products` → puente y triggers
  FTS con `group_concat` de todas las categorías. Aplicada en la BD de pruebas
  (cada `apply_migrations`) y en `data/cuadra.db` (13 filas en el puente).
- `domain/catalog.py`: `product_category_ids`, `product_category_names`,
  `_attach_product_categories`, `_sync_product_categories`; `list_products`,
  `get_product` y `llm_product_json` incluyen `categories`/`categorias`.
  `create_product`/`update_product` escriben el puente. Nuevos:
  `search_catalog`, `check_stock`, `available_units`, `availability_of`,
  `product_highlights`, `_pick_category`, `_products_in_categories`,
  `_narrow_by_qualifiers`, `_score_and_sort`.

**Tools y turno:**

- `ai/tools/catalog.py`: `catalog.search`, `catalog.get`,
  `catalog.list_category`, `catalog.check_stock`; `SearchArgs` con
  `include_out_of_stock` y `category_hint`; salida `CatalogProduct` sin
  economía interna.
- `ai/tools/base.py`: `_result` extrae evidencia de `data["products"]` y
  conserva `empty_reason`; una lectura vacía ya no se confunde con error.
- `ai/turn/render.py`: `_remember_listing` guarda `last_listing` al responder
  con tabla, hermanos, alternativas o el listado de categoría.
- `ai/turn/policy.py`: `last_listing` resuelve «esos»; `resolve_reference`
  enciende ordinales, demostrativos y foco; una referencia ambigua pregunta.
  **Corrección de la prueba del dueño:** un listado explícito o una referencia
  etiquetados por el NLU como `otro` ya no caen al compositor; se responden
  desde el catálogo. Antes, «¿qué accesorios tienes?» con `otro` contestaba
  «Cuadra no tiene accesorios».
- `ai/orchestrator.py`: lectura única `catalog.search_catalog` por turno,
  `frame.evidence` con `catalog.search`/`catalog.list_category`/`catalog.get`,
  `pending_offer` con `product_ids` y `query`.
- `ai/offers.py`: `detect` acepta `product_ids` y `query`.
- `ai/grounding.py`: una negación se sustenta con `empty_reason`, con
  `availability="out_of_stock"` o con stock 0; «no hay problema» no cuenta.
- `config.py`: `AGENT_TOOLS_CATALOG_MODE=off` (solo gobierna las tools que
  proponga el modelo en L3.6; la ruta determinista lee el dominio directamente).

**Tests:**

- `tests/test_catalog_tools.py` (20): migración y triggers, multi-categoría,
  «accesorios para laptop», 4/6/12/10 sin relleno, agotados, `no_category`,
  `category_empty`, orden por stock como desempate, contrato de las 4 tools.
- `tests/test_turns.py`: recordar el listado, «el segundo», «esos», listado
  durante una venta, y los dos casos nuevos con intención `otro`.
- `tests/test_tools_contract.py` y `test_hardening.py` actualizados al contrato.
- Suite completa: **553 tests OK**; `check_all.ps1 -SkipBuild` en verde.

**Replay sintético (20 conversaciones / 30 turnos):** con la corrección, las
conversaciones de catálogo listan productos reales y «el segundo»/«el de antes»
resuelven contra el último listado. La comparación estricta contra `main`
deja de aplicar en L3.1 porque el catálogo cambia a propósito.

**Pendiente:**

- Panel: selector múltiple de categorías con la primaria marcada aparte (§5.2).
  La API ya acepta `category_ids`; la pantalla sigue con un solo `category_id`.
- Revisión del dueño: casos concretos que encontró en su prueba.
- Push y decisión de hacer público el repositorio.

---

## 6. L3.2 — Ventas, ofertas, zonas y fechas

### 6.1 Objetivo

Que el bot no invente estado de pedido, cobertura ni fecha de entrega.

### 6.2 Tools

| Tool | Entrada | Salida | Nota |
|---|---|---|---|
| `sales.status` | `{sale_id?, order_code?, conversation_id?}` | `{sale_id, order_code, status, product, created_at, delivered_at?, cancelled_at?}` | lee `domain/sales` |
| `sales.history` | `{conversation_id, limit=5}` | lista de ventas | post-venta |
| `offers.pending` | `{conversation_id}` | `{kind, product_id, product_ids, query}` | vista del frame |
| `zones.check` | `{product_id, zone}` | `{zone_id, zone_name, covered, source, effective_zones}` | `resolve_zone` + `product_covers_zone` |
| `zones.list` | `{product_id}` | `{zones: [...]}` | cobertura efectiva |
| `logistics.eta` | `{product_id, zone_id?}` | `{eta, source, as_of}` | solo si el TCP dio el dato |

`logistics.eta` devuelve `empty_reason="no_tcp_eta"` si no hay dato. La política
nunca convierte eso en una fecha estimada por el modelo.

### 6.3 Grounding

- Estado de pedido: requiere `sales.status` del turno.
- Cobertura: requiere `zones.check` del turno.
- Fecha: requiere `logistics.eta` con `source="tcp"`.
- Si no hay evidencia, la respuesta dice «no tengo confirmada la fecha» o «no
  puedo confirmar la cobertura», no una promesa.

### 6.4 Tests

- Post-venta: «¿ya llegó?», «¿cuándo llega?», «¿puedo cancelar?».
- Zona cubierta / no cubierta / zona desconocida.
- Fecha con TCP / sin TCP.
- Oferta pendiente: «sí» a alternativas lista las correctas; «no» no reserva.
- Una venta no se cancela dos veces; una escalación no se duplica.

### 6.5 Criterios de aceptación

- [x] Toda afirmación de estado, zona o fecha tiene evidencia.
- [x] Las escrituras siguen pasando solo por la máquina de estados.
- [x] Sin regresiones en ventas, cancelaciones y despacho.
- [x] `agent.tools.sales.mode=off` mantiene el comportamiento actual.

### 6.6 Estado de implementación (2026-11-20)

- `ai/tools/sales.py`: `sales.status`, `sales.history`, `offers.pending`,
  `zones.check`, `zones.list` y `logistics.eta`, todas de lectura y con
  `empty_reason` estable (`no_sale`, `no_pending_offer`, `unknown_zone`,
  `no_zones`, `no_tcp_eta`).
- `domain/sales.list_sales` acepta `conversation_id` para el historial.
- `ai/tools/base._result` extrae evidencia de ventas, zonas y ETA.
- `ai/orchestrator._sales_evidence` registra en `frame.evidence` la venta, la
  cobertura y la ETA que respaldan el turno; la ruta determinista sigue
  llamando al dominio directamente.
- `ai/grounding.py`: una afirmación de estado exige `sales.status`; una de
  cobertura exige `zones.check`/`zones.list`; una fecha exige
  `logistics.eta` con `source="tcp"`. Sigue en `shadow`.
- `config.py`: `AGENT_TOOLS_SALES_MODE=off` (solo gobierna las tools que
  proponga el modelo en L3.6).
- Tests: `tests/test_sales_tools.py` (26) más un caso de turno que verifica la
  evidencia. Suite completa: **580 tests OK**; `check_all.ps1 -SkipBuild` en
  verde.
- `logistics.eta` devuelve `no_tcp_eta` mientras no exista un campo de fecha
  confirmada por el TCP; nunca se convierte en una fecha estimada por el
  modelo.

---

## 7. L3.3 — Memoria y cliente

### 7.1 Tools

| Tool | Entrada | Salida |
|---|---|---|
| `memory.facts` | `{conversation_id}` | `{key: value}` con `updated_at` |
| `memory.profile` | `{customer_id}` | preferencias y últimas compras |
| `memory.summary` | `{conversation_id}` | resumen redactado |

### 7.2 Grounding

- «Ya me dijiste tu dirección» requiere un hecho fijado.
- «No me has dicho el nombre» requiere ausencia del hecho.
- Una corrección del cliente siempre gana: si dice «mi dirección es otra», se
  actualiza el hecho; el grounding no puede bloquear la corrección.
- El perfil no se inyecta si no aporta; se mantiene la regla de PII mínima.

### 7.3 Tests

- Hecho fijado / no fijado.
- Corrección de un hecho.
- Perfil entre conversaciones.
- Resumen sin términos internos.

### 7.4 Criterios de aceptación

- [x] No se contradice un hecho vigente.
- [x] Una corrección se persiste y se usa en el siguiente turno.
- [x] Sin fugas de PII en trazas.

### 7.5 Estado de implementación (2026-11-21)

- `ai/tools/memory.py`: `memory.facts`, `memory.profile` y `memory.summary`,
  de solo lectura, con `empty_reason` (`no_facts`, `no_profile`, `no_summary`).
- `ai/orchestrator._memory_evidence` registra en `frame.evidence` solo las
  **claves** de los hechos vigentes, no sus valores: el frame no transporta PII.
- `ai/grounding.py`: «ya me dijiste X» exige el hecho presente; «no me has
  dicho X» exige su ausencia. Una corrección («mi dirección es otra») no
  coincide con esos patrones y nunca se bloquea. Sigue en `shadow`.
- `ai/tools/base._trace` redacta teléfonos, correos y direcciones del resultado
  de la tool antes de escribir la traza (D7); el resto del trazado no cambia.
- `config.py`: `AGENT_TOOLS_MEMORY_MODE=off`.
- Tests: `tests/test_memory_tools.py` (17): hechos, corrección, perfil entre
  conversaciones, resumen, evidencia sin valores y PII en trazas.
- Suite completa: **597 tests OK**; `check_all.ps1 -SkipBuild` en verde.

---

## 8. L3.4 — Escalaciones y TCP

### 8.1 Tools

| Tool | Entrada | Salida |
|---|---|---|
| `escalations.status` | `{escalation_id? , product_id?}` | `{status, question, answer, supplier, created_at, answered_at?}` |
| `escalations.pending` | `{conversation_id}` | lista de escalaciones abiertas |

### 8.2 Grounding

- «El proveedor dijo que sí» requiere una escalación respondida.
- «Ya consulté» requiere una escalación creada.
- «No me han respondido» requiere una escalación pendiente.

### 8.3 Tests

- Pendiente / respondida / timeout.
- No duplicar escalación.
- No prometer una respuesta que no llegó.

### 8.4 Estado de implementación (2026-11-21)

- `domain/escalations.latest_for_product`: última consulta de un producto.
- `ai/tools/escalations.py`: `escalations.status` (por id o producto) y
  `escalations.pending` (consultas abiertas de la conversación), de solo
  lectura y con `empty_reason="no_escalation"`.
- `ai/orchestrator._escalation_evidence` añade al frame las consultas abiertas
  y el estado de la última consulta del producto, sin el texto de la pregunta.
- `ai/grounding.py`: «ya me confirmó el almacén» exige una consulta respondida;
  «ya lo consulté» exige una consulta creada; «sigo esperando / no me han
  respondido» exige una consulta PENDING. Una oferta condicional («lo consulto
  con el almacén») y un futuro («te confirmo en un rato») no cuentan como
  afirmación. Sigue en `shadow`.
- `config.py`: `AGENT_TOOLS_ESCALATIONS_MODE=off`.
- Tests: `tests/test_escalation_tools.py` (17): estado, pendientes, no
  duplicado y grounding de las tres afirmaciones.
- Suite completa: **614 tests OK**; `check_all.ps1 -SkipBuild` en verde.

---

## 9. L3.5 — Conocimiento y políticas (opcional)

### 9.1 Condición

Solo si existe un corpus curado de políticas (garantía, transporte, pago,
devoluciones). Sin corpus, se difiere y se documenta.

### 9.2 Tool

| Tool | Entrada | Salida |
|---|---|---|
| `knowledge.search` | `{query, limit=3}` | `{docs: [{id, title, text, source}], empty_reason}` |

### 9.3 Grounding

- «La garantía cubre X» requiere un documento.
- «El transporte está incluido» requiere la política vigente
  (`delivery.transport_policy`), no un documento viejo.

### 9.4 Criterios de aceptación

- [ ] Toda afirmación de política tiene fuente.
- [ ] Si no hay corpus, la respuesta deriva al operador; no improvisa.

---

## 10. L3.6 — Tool calling del modelo (shadow → on)

### 10.1 Objetivo

Permitir que el modelo **proponga** lecturas, sin darle escrituras. Primero se
mide en `shadow`; solo después se enciende.

### 10.2 Diseño

- `llm.plan_tools(messages, allowed_tools) -> ToolCall[]` con JSON Schema.
- El modelo recibe nombre, descripción y esquema de entrada de cada tool
  permitida; no recibe SQL ni el catálogo completo.
- La política determinista sigue corriendo como fallback y como validador.
- `agent.tools.mode`:
  - `off`: no se llama al planner; ruta determinista.
  - `shadow`: se llama, se registra la propuesta y se compara; se ejecuta la
    ruta determinista.
  - `on`: se ejecuta la propuesta si pasa validación; si no, fallback.
- Sin tope de iteraciones. El watchdog es el único límite.
- Las tools de escritura no están en `allowed_tools`; el registry las rechaza.

### 10.3 Golden set de tools

`tests/golden/tools.jsonl`:

```json
{"id":"t01","text":"¿qué laptops tienes?","history":[],"expected_tool":"catalog.search","expected_args":{"query":"laptops"},"must_not_call":["sales.status"]}
{"id":"t02","text":"¿y la segunda?","history":[{"role":"assistant","content":"1. ... 2. ..."}],"expected_tool":"catalog.get","expected_args":{"product_id":15}}
{"id":"t03","text":"¿llega a Marianao?","history":[],"expected_tool":"zones.check","expected_args":{"zone":"Marianao"}}
```

### 10.4 Criterios de aceptación

- [ ] `tool_selection_accuracy` ≥ 90 % en el golden set.
- [ ] `tool_args_accuracy` ≥ 90 % en el golden set.
- [ ] 0 escrituras ejecutadas por el modelo.
- [ ] 0 regresiones en la suite.
- [ ] El modo se puede volver a `off` sin reiniciar el flujo (setting).
- [ ] Si no se cumple, se queda en `shadow` y se documenta.

---

## 11. L3.7 — Endurecimiento, evaluación y runbook

### 11.1 Entregables

- `tests/golden/tools.jsonl`, `grounding.jsonl`, `references.jsonl`.
- Ampliación de `tests/golden/nlu.jsonl` con peticiones de catálogo en medio de
  una conversación.
- `scripts/eval_agent.py` y `scripts/eval_tools.py` con las métricas nuevas.
- Dashboard: tarjeta «Herramientas» (llamadas/turno, top tools, errores, cache
  hit, violaciones de grounding, watchdog).
- Runbook: cómo apagar una tool, cómo volver a `off`, cómo leer una traza, cómo
  reproducir un fallo.
- Pruebas de carga con el modelo local y el watchdog.
- Pruebas de seguridad: inyección, fuga de prompt, PII, tool inventada.

### 11.2 Criterios de aceptación

- [ ] Todas las métricas de L3.1–L3.6 medidas y documentadas.
- [ ] `check_all.ps1 -SkipBuild` verde.
- [ ] Runbook probado por alguien que no escribió el código.
- [ ] Sin regresiones en 20 conversaciones reales.
- [ ] El dueño aprueba el estado final y decide el merge a `main`.

---

## 12. Migraciones y datos

| Migración | Contenido | Rollback |
|---|---|---|
| `024_product_categories.sql` | tabla puente, backfill, triggers FTS, reindex | `DROP TABLE product_categories`; restaurar triggers de 013; reindexar |
| — | `last_listing` y `pending_offer` viven en `context_json` | no requiere migración; se pueden borrar las claves |
| — | trazas de tools en `traces` | no requiere migración |

No se crea una tabla `tool_calls` en L3. Si más adelante hace falta consultar
argumentos con índices, se añade una migración aditiva; hoy `traces` alcanza.

### 12.1 Defaults de operación (independiente de L3.1)

Para que borrar la BD no obligue a configurar el panel de Finanzas:

| Setting | Default nuevo | Efecto |
|---|---|---|
| `sales.mode` | `autonomous` | el bot despacha sin aprobación; el operador cobra al final |
| `sales.followup.mode` | `shadow` | el seguimiento se ve en el panel; **no** escribe al cliente |

Cambios:

- `domain/sales.py`: `sale_mode` cae a `autonomous` si no hay fila o el valor es
  desconocido.
- `domain/settings.py`: `SALES_FOLLOWUP_DEFAULT = "shadow"`.
- `migrations/014_sales_and_finance.sql`: la semilla de `sales.mode` pasa a
  `autonomous`; solo afecta a bases nuevas (una BD existente ya aplicó esa
  migración y conserva lo que el operador haya elegido).
- `dashboard/.../finance_panel.tsx`: estados iniciales `autonomous` y `shadow`;
  la descripción de «Asistido» deja de decir que es el modo por defecto.
- Tests: el fixture de ventas y el de hardening fijan `assisted`/`off`
  explícitamente para los casos que prueban ese flujo; tests nuevos verifican
  los defaults de una BD recién migrada y el fallback cuando falta la fila.
- El panel sigue mostrando ambos selectores; no se elimina la posibilidad de
  volver a `assisted`/`off`.

**Estado (2026-11-19):** implementado. Suite completa: **481 tests OK**;
`npm run typecheck` del dashboard OK. No se ha hecho commit ni push.

---

## 13. Evaluación

### 13.1 Sets

| Set | Qué cubre | Corte |
|---|---|---|
| `nlu.jsonl` (58+) | intención, producto, confianza | L3.0 |
| `compose.jsonl` (12+) | redacción, precios, specs | L3.0 |
| `references.jsonl` | «esos», «el segundo», «el de antes» | L3.1 |
| `grounding.jsonl` | negaciones, stock, zona, fecha, estado | L3.1–L3.2 |
| `tools.jsonl` | selección y args | L3.6 |
| `products.json` | productos de apoyo | ya existe |

### 13.2 Umbrales

| Métrica | Umbral para `on` |
|---|---|
| `negative_claim_false_positive` | 0 en el set |
| `tool_selection_accuracy` | ≥ 90 % |
| `tool_args_accuracy` | ≥ 90 % |
| `reference_accuracy` | ≥ 95 % |
| `listing_size_ok` | 100 % |
| `no_duplicate_products` | 100 % |
| `grounding_violation_rate` | ≤ 2 % en turnos reales etiquetados |
| `watchdog_rate` | ≤ 0.5 % |
| `p95_latency` | informativo; no bloquea |

### 13.3 Replay

- Antes de L3.1: 20 conversaciones reales, comparar respuestas.
- Después de cada corte: mismas 20 + los casos nuevos.
- Cualquier diferencia se explica; si es una mejora, se aprueba; si es una
  regresión, se corrige antes de seguir.

---

## 14. Observabilidad y watchdog

### 14.1 Trazas

- Raíz: `pipeline.handle_turn` (se mantiene el nombre).
- Fases: `orchestrator.assemble`, `orchestrator.plan`, `orchestrator.read`,
  `orchestrator.decide`, `orchestrator.compose`, `orchestrator.persist`.
- Tools: `tool.catalog.search`, `tool.catalog.get`, `tool.sales.status`, etc.
- Campos de tool: `args` redactados, `ok`, `count`, `empty_reason`, `source`,
  `as_of`, `cache_hit`, `duration_ms`, `turn_id`, `conversation_id`.

### 14.2 Configuración

| Clave | Default | Qué hace |
|---|---|---|
| `AGENT_TURN_WATCHDOG_S` | `180` | detector de cuelgue; `0` desactiva |
| `LLM_TIMEOUT_S` | `120` | timeout de una llamada al modelo |
| `AGENT_TOOL_TIMEOUT_S` | `5` | timeout de una lectura local |
| `AGENT_TOOLS_MODE` | `off` | modo global de tools |
| `AGENT_TOOLS_CATALOG_MODE` | `off` | modo por dominio |
| `AGENT_TOOLS_SALES_MODE` | `off` | modo por dominio |
| `AGENT_TOOLS_MEMORY_MODE` | `off` | modo por dominio |
| `AGENT_TOOLS_ESCALATIONS_MODE` | `off` | modo por dominio |
| `AGENT_TOOLS_CACHE_TTL_S` | `0` | cache entre turnos (0 = solo turno) |
| `AGENT_GROUNDING_MODE` | `shadow` | `off`/`shadow`/`on` |

### 14.3 Watchdog

- No limita iteraciones ni tools.
- Si se dispara: cancelar, trazar `turn_watchdog`, plantilla segura, marcar turno
  terminado, no reintentar.
- Test dedicado con un handler que duerme más que el watchdog.

---

## 15. Riesgos y mitigación

| Riesgo | Prob. | Impacto | Mitigación |
|---|---|---|---|
| Deriva de comportamiento al extraer | Media | Alto | L3.0 sin capacidades; suite + replay; alias. |
| Tests acoplados a `pipeline` | Alta | Medio | Alias; migración gradual. |
| Migración de categorías rompe FTS | Media | Alto | Triggers nuevos + reindex + test en BD de prueba. |
| Ranking multi-categoría duplica productos | Media | Medio | Dedupe por id; test explícito. |
| Negación mal validada | Media | Alto | Set de negaciones; `shadow` antes de `on`. |
| Watchdog corta turnos válidos | Baja | Medio | Default alto; calibrar con p95 real. |
| Doble respuesta por timeout | Baja | Alto | Turno marcado como terminado; sin reintento. |
| Latencia alta por más lecturas | Media | Medio | Calidad primero; cache por turno; watchdog. |
| Fuga de datos internos en tools | Baja | Alto | `redact`; `llm_product_json`; tests de fuga. |
| El modelo propone tools inexistentes | Media | Bajo | Registry cerrado; validación de args; fallback. |
| Sobreingeniería | Media | Medio | Contrato mínimo; solo lecturas; sin framework externo. |

---

## 16. Definición de terminado (por corte)

Un corte está terminado cuando:

1. El código está en su módulo, con nombres en inglés y comentarios claros.
2. Los tests nuevos cubren el caso feliz, el vacío, el ambiguo y el error.
3. La suite completa y `check_all.ps1 -SkipBuild` están verdes.
4. La documentación del corte está actualizada (este plan + bitácora).
5. Hay trazas de las tools y del watchdog.
6. El modo `off` mantiene el comportamiento anterior.
7. El replay de conversaciones no tiene regresiones sin explicar.
8. El dueño revisa y decide el merge.

---

## 17. Checklist antes de merge a `main`

- [ ] Rama con nombre `feat/l3-*`.
- [ ] Sin secretos en el diff.
- [ ] Sin archivos temporales ni `load_test_results*.json` trackeados.
- [ ] Suite completa verde.
- [ ] `check_all.ps1 -SkipBuild` verde.
- [ ] Migración probada en BD de prueba y rollback documentado.
- [ ] Replay de 20 conversaciones sin regresiones.
- [ ] Documentación actualizada.
- [ ] El dueño aprueba el merge en el momento.
- [ ] No se hace push salvo pedido explícito.

---

## 18. Próximo paso inmediato

L3.0–L3.3 están mergeados en `main`. **L3.4 está implementado en el árbol de
trabajo** y la suite queda en **614 tests** verdes, pendiente de commit.

1. Revisar y commitear L3.4.
2. L3.5 (conocimiento) es opcional; si no hay corpus, el siguiente corte es
   **L3.6**: tool calling del modelo en `shadow` → `on`.
3. No hacer push ni merge sin pedido explícito.

---

## 19. Lo que no se hace en L3

- No se reescribe la máquina de estados de ventas.
- No se cambia el modelo por uno más grande.
- No se añaden tools de escritura.
- No se implementa venta multi-producto.
- No se hace RAG si no hay corpus.
- No se envían mensajes de «espera» al cliente.
- No se hace push ni merge sin aprobación explícita.
