---
tipo: vision_y_decisiones
proyecto: Cuadra
modulo: agente_ia
estado: DECISIONES BASE TOMADAS — pendiente de revisión final del dueño
revisado: 2026-11-19
tags: [agente, capacidades, tools, grounding, orquestador, turn-frame, decisiones]
---

# 14 — Visión y capacidades del agente (L3)

> **Qué es este documento.** Fija el modelo operativo del agente antes de escribir
> la capa de lectura: qué capacidades existen, cómo se declaran, quién decide, quién
> ejecuta, cómo se prueba y qué no entra. El plan ejecutable está en
> `15_PLAN_FASE_L3.md`.
>
> **Qué NO es.** No es una refactorización cosmética de `pipeline.py` ni una lista de
> parches para la prueba #8. Es el cambio de modelo que evita que cada error nuevo se
> arregle con otra cláusula en `_decide`.

---

## 0. Resumen en una página

El agente actual funciona con un patrón frágil:

1. El NLU clasifica la intención.
2. `pipeline._run_turn` resuelve un producto con funciones deterministas.
3. `pipeline._decide` es una cadena de `if` por dominio (catálogo, stock, venta,
   zona, logística, post-venta, banal).
4. El LLM redacta cuando la plantilla no alcanza.
5. Los guardrails validan formato, precio y specs.

El hueco es el paso 4: **el modelo puede afirmar un hecho que nadie leyó en ese
turno**. La prueba #8 lo demostró con «no hay accesorios para laptop»; el mismo
patrón puede producir «no hay stock», «no llega a tu zona», «tu pedido ya se
entregó» o «el proveedor dijo que sí». No es un bug de catálogo: es un bug de
**procedencia del dato**.

L3 cambia el contrato:

- **El LLM interpreta y redacta; el harness decide y ejecuta.**
- Toda capacidad es una **tool de lectura** con entrada tipada, salida tipada,
  procedencia y errores estables.
- Toda afirmación factual se valida contra la **evidencia de ese turno**.
- Las escrituras siguen exclusivamente en la máquina de estados; el modelo nunca
  escribe.
- El **orquestador** secuencia el turno y garantiza los invariantes.
- El **TurnFrame** conserva lo que el cliente ya dijo: producto en juego, último
  listado, oferta pendiente, venta activa, hechos y perfil. Sin él, «esos» o «el
  segundo» no se pueden resolver y el catálogo se rompe en la segunda frase.

La fase se llama **L3 — Capa de lectura del agente (el catálogo como herramienta)**.
No la llamamos «refactorización» porque añade una capa nueva; la extracción del
orquestador es el soporte de esa capa, no el objetivo.

---

## 1. El fallo que originó esto

### 1.1 La prueba #8

Conversación real:

> Cliente: «¿Qué productos tienes de accesorios para laptop?»
> Bot: listó Mouse, Audífonos y Teclado, pero **también incluyó la laptop**.
> Cliente: «¿Qué procesador trae la laptop?»
> Bot: «Ryzen 7».

El listado no era un error de formato: el agente había heredado el producto del
turno anterior y lo trató como parte de la categoría. El fix (`7e0ba66`) añadió
`_catalog_request`, una ruta de catálogo y `clear_product`, y funcionó. Pero el fix
es una cláusula más en una cadena de cláusulas. Si mañana falla «¿tienen algo para
la playa?», añadiremos otra.

### 1.2 El patrón general

| Síntoma real | Causa de fondo | Dominio |
|---|---|---|
| «No hay accesorios para laptop» con productos reales | el modelo negó sin leer el catálogo | catálogo |
| «No hay stock» cuando sí hay | el stock se infirió del contexto, no se leyó | stock |
| «Sí llega a tu zona» sin consultar cobertura | la zona se prometió sin leer `product_zones` | logística |
| «Tu pedido ya se entregó» sin leer la venta | el estado se dedujo de la conversación | ventas |
| «El proveedor dijo que sí» sin respuesta del TCP | la escalación se inventó | TCP |
| «Ya me dijiste tu dirección» sin hecho fijado | la memoria se asumió | memoria |
| «La garantía cubre X» sin política | no existe corpus de políticas | conocimiento |

Todos comparten la misma forma: **una afirmación factual sin una lectura del turno
que la respalde**. La solución no es un guardrail por frase; es un contrato de
capacidad y un validador de afirmaciones.

---

## 2. Diagnóstico: por qué el diseño actual no escala

| Hecho del código | Consecuencia |
|---|---|
| `backend/cuadra/ai/pipeline.py` tiene ~2.750 líneas y más de 90 definiciones de nivel superior. | Cada dominio nuevo añade ramas a `_run_turn` y `_decide`; el archivo es el cuello de botella. |
| `_decide` (líneas ~1850–2262) es una cadena de `if` por intención y estado. | El orden de las ramas es un invariante implícito: mover una cambia el comportamiento. |
| Las lecturas están dentro de los handlers (`catalog.siblings`, `sales.get`, `catalog.resolve_zones`). | No hay procedencia uniforme ni forma de saber qué dato usó el compositor. |
| El contexto es un `dict` mutable (`conversations.context_json`). | No hay `last_listing`, no hay payload de la oferta, no hay referencias; cada handler inventa su clave. |
| `offers` guarda `{kind, product_id}`. | «Sí» a una lista de alternativas no sabe **cuáles** alternativas. |
| `guardrails.validate_reply` valida precio, specs, formato y fugas. | No valida negaciones («no hay X»), stock, zonas, fechas ni estado de pedido. |
| `pipeline.py` no tiene un orquestador explícito. | El orden de las fases vive en la cabeza de quien escribió `_run_turn`. |
| Los tests importan funciones privadas de `pipeline` (`_resolve_product`, `_maybe_handle_sale`, etc.). | Cualquier extracción debe conservar alias o migrar los tests. |
| El golden set mide NLU y redacción. | No mide selección de tool, argumentos, referencias ni grounding. |

La deuda no es «falta una función de catálogo». Es **falta un contrato**.

---

## 3. Principios de diseño

1. **El LLM interpreta y redacta; el harness decide y ejecuta.** El modelo puede
   proponer una lectura; no ejecuta escrituras, no calcula precios, no decide
   cobertura, no inventa stock.
2. **Los invariantes viven en código.** Precio, stock, zona, estado de venta,
   comisión y margen se calculan con funciones deterministas.
3. **Toda capacidad se declara igual.** Nombre, entrada, salida, permisos, dueño,
   política de error y política de cache.
4. **Toda afirmación factual necesita evidencia del turno.** Precio, stock, zona,
   fecha, estado de pedido, existencia y specs.
5. **Las escrituras solo pasan por la máquina de estados.** Las tools de L3 son
   de lectura. Una tool de escritura futura tendrá permiso explícito y aprobación.
6. **Primero el contrato, después la primera implementación.** El catálogo es el
   caso que valida el contrato, no el contrato entero.
7. **Modos por capacidad.** `off` / `shadow` / `on`, como `sales.followup.mode`:
   primero se observa, se mide y luego se enciende.
8. **Calidad antes que latencia.** No se limita el número de lecturas ni de
   iteraciones. Solo se interrumpe un turno cuando se colgó (watchdog), no cuando
   «tarda lo normal».
9. **Una respuesta por turno.** Un timeout, un reintento o un reinicio no pueden
   producir dos mensajes al cliente.
10. **Español en la documentación y la UI; inglés en el código.**

---

## 4. Mapa de capacidades

Cada fila es una capacidad que el agente necesita. «Lectura» es lo que L3 añade;
«escritura» es lo que ya existe o se mantiene en la máquina de estados.

| Dominio | Lecturas (tools) | Escrituras | Estado actual | Riesgo si no se cubre | Corte |
|---|---|---|---|---|---|
| **Catálogo** | `search_catalog`, `get_product`, `list_category`, `check_stock` | mutaciones TCP (existen) | funciones deterministas, routing parcial | «no hay X» falso; listados incompletos | L3.1 |
| **Ventas / pedido** | `get_sale_status`, `get_pending_offer`, `get_sale_history` | crear, avanzar, cancelar (máquina) | `_post_sale_outcome` lee BD | inventar estado, fecha o entrega | L3.2 |
| **Zonas / cobertura** | `check_zone`, `list_zones` | — | `_zone_coverage_outcome` | prometer una zona que el producto no cubre | L3.2 |
| **Logística / fechas** | `get_delivery_eta` (solo si el TCP la dio) | — | regla determinista sin dato | prometer un plazo | L3.2 |
| **Ofertas** | `get_pending_offer` | consumir oferta | `offers.py` | repetir o perder una reserva | L3.2 |
| **Memoria / cliente** | `get_facts`, `get_customer_profile`, `get_history_summary` | capturar hechos | `memory.py` + `ContextBuilder` | contradecir lo ya dicho | L3.3 |
| **Escalaciones / TCP** | `get_escalation_status`, `get_pending_escalations` | crear/escalar | `escalations` | escalar dos veces o no escalar | L3.4 |
| **Conocimiento / políticas** | `search_docs` | — | no existe | improvisar garantía, transporte o pago | L3.5 |

**Orden real de trabajo.** No es «catálogo y después el resto». Primero va el
**marco del turno** (L3.0), porque el catálogo depende de lo que el cliente ya
dijo: «esos», «el segundo», «el de antes», «el mismo». Sin ese marco, el listado
multi-categoría funciona en el primer mensaje y falla en el segundo.

---

## 5. El contrato de capacidad

### 5.1 ToolSpec

Toda tool se declara con la misma forma. No hay handlers ad hoc.

```python
# backend/cuadra/ai/tools/base.py
from dataclasses import dataclass, field
from typing import Any, Literal
from pydantic import BaseModel


@dataclass(frozen=True)
class ToolSpec:
    name: str                         # "catalog.search"
    domain: str                       # "catalog"
    description: str                  # para el planner y la documentación
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    permission: Literal["read", "write"] = "read"
    owner: str = "catalog"            # módulo de dominio dueño del dato
    cache_ttl_s: int = 0              # 0 = cache solo dentro del turno
    timeout_s: float = 5.0            # lectura local; 5 s ya es un bug
    redact: tuple[str, ...] = ()      # campos que nunca salen al prompt
    mode_key: str | None = None       # "agent.tools.catalog.mode"
```

### 5.2 ToolResult

El compositor y el validador necesitan saber **qué es dato** y **qué es
redacción**. Un `dict` suelto no alcanza.

```python
@dataclass
class ToolResult:
    ok: bool
    data: Any
    source: str                       # "catalog.products", "sales", "tcp", "memory"
    as_of: str                        # ISO-8601 del momento de la lectura
    confidence: float = 1.0
    evidence: list[dict] = field(default_factory=list)  # ids/campos usados
    empty_reason: str | None = None   # "no_match", "category_empty", "no_stock"
    error: "ToolError | None" = None
    cache_hit: bool = False
```

Reglas:

- `ok=True` con `data=[]` **no es un error**: es una lectura vacía con una razón.
  `empty_reason` es lo que permite afirmar «no hay X» con fundamento.
- `ok=False` solo para fallos de ejecución (`invalid_args`, `unavailable`,
  `timeout`, `forbidden`).
- `source` y `as_of` viajan al prompt y a la traza. El compositor puede citar
  «según el catálogo de ahora», no «según lo que dijo el modelo».
- `evidence` guarda los ids y campos que sostienen la respuesta. Sin evidencia,
  no hay afirmación.

### 5.3 Errores estables

| Código | Cuándo | Qué hace la política |
|---|---|---|
| `invalid_args` | la entrada no valida contra el modelo | no se ejecuta; se registra; se usa la ruta determinista |
| `not_found` | el recurso pedido no existe | se responde con la plantilla de «no encontré» |
| `ambiguous` | hay más de una coincidencia | se pregunta, nunca se adivina |
| `unavailable` | la BD o el servicio no responde | fallback seguro; se registra |
| `timeout` | superó `timeout_s` | fallback seguro; se registra |
| `forbidden` | la tool no está permitida en ese modo | no se ejecuta; se registra |

### 5.4 Modos

| Modo | Qué hace | Uso |
|---|---|---|
| `off` | la ruta determinista decide y ejecuta; el modelo no propone tools | estado inicial de cada capacidad |
| `shadow` | el modelo propone tool y argumentos; **no se ejecutan**; se comparan con la ruta determinista | medición sin riesgo |
| `on` | el modelo puede elegir entre tools de lectura permitidas; la política valida args y evidencia | solo tras métricas |

Claves de settings:

- `agent.tools.mode` = `off` | `shadow` | `on` (global).
- `agent.tools.catalog.mode`, `agent.tools.sales.mode`, etc. (por dominio).
- Una capacidad nueva nace en `off`; `shadow` no cambia la respuesta.

### 5.5 Cache por turno

No es un límite de latencia. Es corrección: si dos partes del turno piden
`get_product(7)`, deben ver el mismo dato. El cache vive en el `TurnFrame`, se
invalida al final del turno y después de una escritura de la máquina de estados.
No se comparte entre turnos salvo `cache_ttl_s > 0` explícito.

### 5.6 Seguridad del contrato

- El modelo **no** puede inventar un nombre de tool: el registry es una lista
  cerrada y el planner solo recibe los `ToolSpec` permitidos.
- Los argumentos se validan con Pydantic; los strings tienen longitud máxima.
- Las tools de lectura no reciben SQL ni nombres de tabla.
- Los campos internos (costo, margen, comisión) se excluyen en `redact` y en
  `llm_product_json`.
- Una inyección en un mensaje del cliente no puede crear una tool ni saltarse la
  política.

**Estado (2026-11-19):** contrato implementado en `ai/tools/base.py`
(`ToolSpec`, `ToolResult`, `ToolError`, `ToolCall`, `Registry` cerrado, cache por
turno, `allowed(mode)`), con `catalog.search`, `catalog.get` y
`catalog.list_category` registradas como prueba. `AGENT_TOOLS_MODE=off` por
defecto: el modelo todavía no propone tools.

---

## 6. El orquestador

### 6.1 Decisión

**El orquestador se implementa en L3.0. No se difiere.** No es un wrapper sobre
`pipeline._run_turn`; es la extracción del ciclo de vida del turno a un módulo con
fases explícitas.

### 6.2 Qué hace y qué no hace

El orquestador **sí**:

- Carga el turno, el historial, el contexto, los hechos, el perfil, la venta
  activa, la oferta pendiente y el último listado.
- Ejecuta el NLU o el fast-path.
- Resuelve producto, foco y referencias.
- Pide a la política un `TurnPlan` (qué lecturas hacen falta y qué decisión
  corresponde).
- Ejecuta las lecturas con cache por turno y recoge evidencia.
- Aplica la decisión (plantilla, ficha, listado, paso de venta, escalación).
- Compone con el LLM solo cuando hace falta.
- Valida guardrails + grounding.
- Persiste contexto, hechos, outcome y trazas.
- Garantiza una respuesta por turno y el watchdog.

El orquestador **no**:

- No decide reglas de negocio (eso es la política de cada dominio).
- No calcula precios ni stock (eso es el dominio).
- No escribe ventas (eso es la máquina de estados).
- No construye prompts a mano (eso es el compositor).
- No conoce detalles de Telegram.

### 6.3 Fases

```
run_turn(turn):
  1. assemble   -> TurnFrame (contexto, historial, hechos, perfil, venta, oferta, listado)
  2. interpret  -> Intent (NLU o fast-path)
  3. resolve    -> foco, producto, referencias, respuesta a oferta
  4. plan       -> TurnPlan (lecturas + decisión candidata)
  5. read       -> ToolResult[] (cache por turno, evidencia)
  6. decide     -> Outcome (listado, ficha, stock, venta, zona, escalación, plantilla)
  7. write      -> solo máquina de estados / ofertas / contexto / hechos
  8. compose    -> plantilla o LLM con evidencia
  9. validate   -> guardrails + grounding
 10. persist    -> contexto, outcome, trazas, métricas
 11. emit       -> reply, escalate, sale, latency
```

### 6.4 Complejidad y riesgo

- **Tamaño:** `pipeline.py` tiene ~2.750 líneas. La extracción es grande, pero
  mayormente mecánica: mover funciones, no reescribirlas.
- **Riesgo principal:** cambiar comportamiento al mover código. Mitigación:
  L3.0 no añade capacidades; se mueve en cortes pequeños; `pipeline.py` conserva
  alias de las funciones que los tests importan; se corre la suite completa y un
  replay de conversaciones reales después de cada corte.
- **Riesgo secundario:** doble traza. Se mantiene `pipeline.handle_turn` como
  nombre de traza raíz (el dashboard ya lo consulta) y se añaden trazas hijas por
  fase; no se duplica el contador.
- **Beneficio:** cada capacidad nueva pasa a ser «un módulo + una entrada en el
  registry + tests», no otra rama en `_decide`.

### 6.5 Estructura objetivo

```
backend/cuadra/ai/
  orchestrator.py          # run_turn: ciclo de vida y watchdog
  turn/
    frame.py               # TurnFrame: estado del turno
    references.py          # "esos", "el segundo", "el de antes"
    policy.py              # plan y decisión determinista por dominio
    composer.py            # prompt + LLM + guardrails
    render.py              # fichas, tablas, plantillas
    sales_flow.py          # pasos de la máquina de estados
  tools/
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

`context.py`, `guardrails.py`, `prompts.py`, `offers.py`, `memory.py` y los
módulos de `domain/` se mantienen; las tools los envuelven.

---

## 7. TurnFrame y referencias conversacionales

### 7.1 Por qué

El cliente no habla en mensajes aislados:

> «¿Qué laptops tienes?»
> «La segunda.»
> «¿Y esa tiene garantía?»
> «Sí, resérvamela.»

Sin un marco del turno, «la segunda» no tiene referente, «esa» no tiene producto y
«resérvamela» no sabe qué reservar. El catálogo multi-categoría no se puede probar
solo con el primer mensaje.

### 7.2 Qué guarda

```python
@dataclass
class TurnFrame:
    conversation: dict
    context: dict
    history: list[dict]
    turn_messages: list[dict]
    text: str
    intent: "Intent | None"
    stage: str
    focus_product: dict | None
    focus_source: str                 # "text", "context", "offer", "listing", "id"
    last_listing: "Listing | None"    # ids + etiquetas + consulta + fecha
    pending_offer: dict | None        # kind + product_ids + query
    active_sale: dict | None
    facts: dict[str, str]
    profile: dict | None
    candidates: list[dict]
    references: list["Reference"]
    evidence: list["ToolResult"]
    cache: dict[str, "ToolResult"]
    started_at: float
    deadline: float | None
```

### 7.3 Persistencia aditiva en `context_json`

No requiere migración; el contexto ya es JSON. Se añaden claves:

```json
{
  "last_listing": {
    "product_ids": [12, 15, 18],
    "labels": ["Mouse Inalámbrico", "Audífonos Bluetooth", "Teclado Mecánico"],
    "query": "accesorios para laptop",
    "kind": "category",
    "created_at": "2026-11-19T10:00:00+00:00"
  },
  "pending_offer": {
    "kind": "alternatives",
    "product_id": 12,
    "product_ids": [12, 15, 18],
    "query": "accesorios para laptop"
  }
}
```

`offers.save` y `offers.consume` se amplían para conservar el payload. La oferta
sigue viviendo **un solo turno**: no se puede responder tres mensajes después.

### 7.4 Resolución determinista

| Expresión | Regla |
|---|---|
| «el primero», «el segundo», «el tercero» | índice sobre `last_listing.product_ids` |
| «el último» | último de `last_listing` |
| «esos», «esos dos», «todos esos» | `last_listing` completo o los primeros N |
| «el de antes», «el mismo», «esa» | `focus_product` si existe; si no, el único de `last_listing` |
| nombre o id explícito | `_resolve_product` actual |
| ambigüedad | se pregunta; nunca se adivina |

La resolución se prueba con un set propio (`references.jsonl`). Si falla, la
política pide aclaración; no inventa.

---

## 8. Grounding: afirmaciones con evidencia

### 8.1 Qué se valida

| Afirmación | Evidencia requerida | Ejemplo de violación |
|---|---|---|
| Precio | producto del turno + `allowed_prices` | precio viejo o inventado |
| Stock | `check_stock` / `get_product` del turno | «sí hay» sin leer stock |
| Zona | `check_zone` del turno | «sí llega» sin cobertura |
| Fecha de entrega | `get_delivery_eta` con dato del TCP | «llega el viernes» sin dato |
| Estado de pedido | `get_sale_status` del turno | «ya se entregó» sin leer |
| Existencia negativa | `search_catalog`/`get_product` con `empty_reason` | «no hay laptops» con laptops |
| Specs | corpus de specs del producto | «Ryzen 9» sin dato |
| Política | `search_docs` (si existe corpus) | «la garantía cubre X» |

### 8.2 Negaciones: el caso de la prueba #8

El validador busca cláusulas como:

- «no tenemos / no hay / no manejo / no manejamos»
- «no me queda / no me quedan»
- «no está disponible / no se consigue»
- «no contamos con / no existe»
- «agotado / sin stock»

y exige una lectura del turno que cubra la entidad negada. Si la lectura devolvió
productos, la negación es una violación. Si devolvió vacío con `empty_reason`, es
una afirmación válida.

Comportamiento:

1. `agent.grounding.mode=shadow`: se registra la violación, no se cambia la
   respuesta. Sirve para medir antes de encender.
2. `agent.grounding.mode=on`: se reintenta con corrección; si insiste, se usa la
   plantilla determinista que dice exactamente lo que la evidencia sostiene.
3. Nunca se inventa una negación para «quedar bien».

### 8.3 Evidencia en el prompt

El compositor recibe un bloque explícito:

```
EVIDENCIA DE ESTE TURNO (solo puedes afirmar esto):
- catalog.search("accesorios para laptop") -> 3 productos: ...
- catalog.get(7) -> Laptop ... stock 2, precio 850 USD
- zones.check("Marianao") -> no cubierta por el producto 7
```

El modelo puede redactar, pero no puede salirse de ese bloque para hechos. Los
guardrails actuales siguen corriendo; el grounding se suma, no los reemplaza.

---

## 9. Evaluación y modos

### 9.1 Lo que hoy se mide

- 58 casos NLU (`tests/golden/nlu.jsonl`).
- 12 casos de redacción (`tests/golden/compose.jsonl`).
- 35 productos de apoyo (`tests/golden/products.json`).
- Suite completa: 478 tests OK; `check_all.ps1 -SkipBuild` verde.

### 9.2 Lo que falta medir

| Métrica | Qué mide | Corte |
|---|---|---|
| `tool_selection_accuracy` | eligió la tool correcta | L3.1+ |
| `tool_args_accuracy` | argumentos normalizados correctos | L3.1+ |
| `grounding_violation_rate` | afirmaciones sin evidencia | L3.1+ |
| `negative_claim_false_positive` | negó existencia con datos reales | L3.1 |
| `reference_accuracy` | «esos», «el segundo», «el de antes» | L3.1 |
| `listing_size_ok` | hasta 10 coincidencias reales, sin relleno | L3.1 |
| `no_duplicate_products` | sin repetidos en un listado | L3.1 |
| `watchdog_rate` | turnos abortados por cuelgue | L3.0+ |
| `fallback_rate` | respuestas deterministas de reserva | L3.0+ |
| `p95_latency` | informativo; no es puerta de calidad | L3.0+ |

### 9.3 Criterio para encender una capacidad

Una capacidad pasa de `off` a `shadow` cuando tiene tests y contrato. Pasa de
`shadow` a `on` solo si:

- el golden set de esa capacidad está completo;
- no hay regresiones en la suite;
- la selección de tool y los argumentos superan el umbral acordado;
- el grounding no produce falsos negativos en el set de negaciones;
- hay una muestra de turnos reales etiquetados;
- el dueño aprueba el encendido.

Si no se cumple, se queda en `shadow` y se documenta. No se enciende «para ver».

---

## 10. Observabilidad y watchdog

### 10.1 Trazas

Se reutiliza la tabla `traces` (019). No hace falta una tabla nueva:

- `method = "tool.catalog.search"`, `"tool.catalog.get"`, `"tool.sales.status"`.
- `input_json`: args redactados.
- `output_json`: resumen del `ToolResult` (`ok`, `count`, `empty_reason`, `source`,
  `as_of`, `cache_hit`).
- `duration_ms`, `ok`, `error`, `conversation_id`, `turn_id`.

El dashboard puede contar llamadas por turno, tools más usadas, errores, cache
hit, violaciones de grounding y watchdogs.

### 10.2 Watchdog, no presupuesto

Decisión explícita:

- **No** se limita el número de iteraciones ni de tools por turno. Si el modelo
  necesita ocho lecturas para responder bien, las hace.
- `LLM_TIMEOUT_S=120` sigue siendo el timeout de transporte de una llamada.
- `TOOL_TIMEOUT_S=5` es el timeout de una lectura local; 5 s ya indica un bug.
- `AGENT_TURN_WATCHDOG_S=180` (configurable, `0` lo desactiva) es el **detector
  de cuelgue** del turno completo. Si se dispara:
  1. se cancela el trabajo en curso;
  2. se registra `turn_watchdog` con la fase y la tool pendiente;
  3. se responde con una plantilla segura;
  4. se marca el turno como terminado;
  5. **no se reintenta** para no duplicar el mensaje.

El watchdog no es un objetivo de rendimiento. Es una red de seguridad para no
dejar al cliente esperando indefinidamente por un bug.

### 10.3 Cache y duplicados

- Cache por turno para lecturas idénticas (misma tool + mismos args).
- Detector de llamadas repetidas: si el modelo pide la misma lectura dos veces,
  se reutiliza el resultado; no se cuenta como error.
- Una escritura invalida el cache de su dominio en ese turno.

---

## 11. Seguridad

| Riesgo | Control |
|---|---|
| Fuga de margen, comisión o costo | `redact` en `ToolSpec`; `llm_product_json` filtra; guardrails de términos internos. |
| Fuga del prompt | `check_prompt_disclosure` se mantiene. |
| Inyección desde un mensaje | las tools son una lista cerrada; los args se validan; el modelo no escribe SQL. |
| PII innecesaria | el perfil y los hechos se inyectan solo si aportan; trazas redactadas 30 días. |
| Escritura no autorizada | L3 no expone tools de escritura; la máquina de estados sigue siendo el único camino. |
| Ban de Telegram | las lecturas son locales; no generan mensajes; el rate limit de envío no cambia. |
| Doble respuesta | una respuesta por turno; watchdog no reintenta; `external_id` idempotente. |

---

## 12. Qué NO entra en L3

- Venta multi-producto (sigue diferida, D5).
- Escrituras autónomas del modelo (precio, stock, venta).
- Function calling nativo del 4B como requisito (se usa router determinista +
  propuesta en `shadow`; el nativo es opcional).
- RAG sobre documentos externos si no hay corpus curado.
- Mensajes de «espera» al cliente (se evalúa después; no es necesario para L3).
- Reescribir `prompts.py` o `guardrails.py` desde cero.
- Un framework de agentes externo.
- Cambiar el modelo por uno más grande para «que acierte».

---

## 13. Decisiones (ADR)

| ID | Decisión | Motivo | Consecuencia |
|---|---|---|---|
| **D11** | **Calidad antes que latencia.** Sin tope de iteraciones ni de tools; solo watchdog de cuelgue. | El dueño prioriza respuestas correctas; un turno lento pero correcto es mejor que uno rápido y falso. | `AGENT_TURN_WATCHDOG_S` es un detector de bug, no un SLA. |
| **D12** | **El orquestador se implementa en L3.0.** | Sin él, cada capacidad nueva vuelve a `_decide`; el usuario prefiere hacerlo bien una vez. | Extracción mecánica de `pipeline.py`, con alias y tests. |
| **D13** | **El TurnFrame y las referencias van antes que el catálogo.** | «Esos», «el segundo» y «el de antes» son parte del caso de catálogo. | L3.0 guarda `last_listing` y el payload de la oferta. |
| **D14** | **Contrato primero, capacidad después.** | Evita reescribir el catálogo cuando lleguen ventas o zonas. | `tools/base.py` se aprueba antes de L3.1. |
| **D15** | **Catálogo multi-categoría con tabla puente.** | Un producto puede estar en `accesorios_laptop` y `accesorios`; la búsqueda debe cruzar. | Migración `024_product_categories`; `products.category_id` sigue como primaria, inferida automáticamente si no se indica. |
| **D16** | **Tools de lectura solamente.** | Las escrituras tienen invariantes de dinero y stock. | El modelo nunca crea, avanza ni cancela una venta. |
| **D17** | **Modos `off`/`shadow`/`on` por capacidad.** | Ya funciona con `sales.followup.mode`; permite medir sin riesgo. | Ninguna tool se enciende sin golden set; grounding nace en `shadow`. |
| **D18** | **`ai/orchestrator.py`, `ai/turn/`, `ai/tools/`; `pipeline.py` queda como fachada.** | Separación clara y tests estables. | Los imports privados de tests se mantienen con alias. |
| **D19** | **Grounding compartido, no guardrails por frase.** | La negación falsa es el mismo bug en seis dominios. | `ai/grounding.py` valida claims contra evidencia del turno. |
| **D20** | **Observabilidad en `traces`, sin tabla nueva.** | La tabla ya tiene method, input, output, duración y turno. | Métricas por `method LIKE 'tool.%'`. |
| **D21** | **Una respuesta por turno también en timeout.** | Un reintento no puede duplicar el mensaje. | El watchdog marca el turno como terminado con plantilla. |
| **D22** | **No se enciende `on` sin muestra real etiquetada.** | El golden set sintético no cubre la operación. | `shadow` puede durar lo que haga falta. |
| **D23** | **Defaults de operación sin setup manual.** `sales.mode=autonomous`, `sales.followup.mode=shadow`. | Al borrar la BD, el dueño no debe volver a configurar el panel de Finanzas. | El modo de venta es automático y el seguimiento se ve en el panel sin escribir al cliente; ambos se pueden cambiar. |

---

## 14. Relación con la documentación existente

| Documento | Qué sigue vigente | Qué cambia con L3 |
|---|---|---|
| `00_ARQUITECTURA_Y_MODELO_AUTONOMO.md` | modos `sales.mode`, estados, guardrails, autonomía configurable | se añade la capa de lectura y el orquestador |
| `01_MEMORIA_RETRIEVAL_Y_CATALOGO.md` | turnos, hechos, resumen, FTS5, embeddings, presupuesto de contexto | el retrieval de productos pasa por `search_catalog` |
| `02_MAQUINA_ESTADOS_VENTAS_Y_FINANZAS.md` | máquina de estados, stock reservado, ledger | las lecturas de venta se exponen como tools; las escrituras no cambian |
| `03_BUCLE_TCP_MUTACION_CATALOGO_Y_ROLLBACK.md` | triage, mutaciones, undo | se añade `get_escalation_status` |
| `04_PLAN_IMPLEMENTACION_Y_PROMPTS_MAESTROS.md` | fases 0–5, contratos, prompts, unittest | L3 es una fase nueva, no una reapertura de 0–5 |
| `13_BITACORA_OPERACION_REAL.md` | pruebas #1–#8 | la prueba #8 es el caso semilla de L3.1 |
| `15_PLAN_FASE_L3.md` | — | plan ejecutable de esta visión |

**No se reescriben los documentos 00–13.** L3 los complementa. Si hay conflicto,
manda `14` + `15` para la capa de lectura y `00`–`12` para lo ya implementado.

---

## 15. Decisiones cerradas (2026-11-19)

1. **Listado:** se muestran **hasta 10** coincidencias reales. Si hay 6, se
   muestran 6; no se rellena con productos que no coinciden. No hay un mínimo
   artificial de 5.
2. **Agotados:** se listan si coinciden, como un `order by`: primero las mejores
   coincidencias; si dos empatan, primero la que tiene stock; si queda capacidad,
   también las agotadas. Nunca se muestra algo que no tenga relación con la
   consulta.
3. **Categoría primaria:** se infiere automáticamente (la primera asignada o la
   más específica). El panel puede permitir cambiarla, pero el operador no está
   obligado a elegirla para crear un producto.
4. **Grounding:** nace en `shadow`; se enciende cuando el golden set de negaciones
   y una muestra real etiquetada estén completos. No hay un número fijo de turnos.
5. **Watchdog:** 180 s por defecto, calibrable con el p95 real. No se usa para
   limitar la calidad.
6. **Defaults de operación:** `sales.mode=autonomous` y
   `sales.followup.mode=shadow` por defecto. Así, al borrar la BD no hay que
   volver a configurar el panel de Finanzas: el modo de venta queda automático y
   el seguimiento se ve en el panel sin escribir al cliente. El panel sigue
   permitiendo cambiarlos.

Estas decisiones no bloquean L3.0. La 1, 2 y 3 se implementan en L3.1; la 4 en
L3.1–L3.2; la 5 en L3.0; la 6 es un cambio de configuración independiente.
