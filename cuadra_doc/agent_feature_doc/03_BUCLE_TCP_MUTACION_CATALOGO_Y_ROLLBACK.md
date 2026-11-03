---
tipo: especificacion_tecnica
proyecto: Cuadra
modulo: bucle_tcp_catalogo
estado: DISEÑO APROBADO — listo para implementar
revisado: 2026-11-03
aprobado: 2026-11-03
revision: 2
tags: [tcp, escalaciones, audio-whisper, undo-log, mutacion-catalogo, auditoria, case-id]
---

# 03 — Bucle TCP, mutación de catálogo y reversión

> **Cambios de esta revisión:** se añadió el triage de mensajes TCP, se corrigió el
> audio TCP (hoy no se transcribe), se usa `[E-###]` derivado de `escalations.id`,
> la ambigüedad ya no se auto-aplica, el undo detecta conflictos, y el esquema usa
> `catalog_mutation_log` real (migración `015`).

---

## 1. Principio: mutación con red de seguridad, no mutación ciega

En el comercio informal cubano los precios y el stock cambian a diario. El bot no puede
esperar a que el operador lea un chat para actualizar el catálogo: vendería stock que ya
no existe o a precios viejos.

Pero la propuesta original aplicaba **cualquier** cambio de precio que el LLM creyera
entender, con un undo como única defensa. Eso es peligroso: un error de audio puede
vender a 5 USD un producto de 50, y el cliente ya pagó antes de que el operador revert.

Modelo corregido:

| Tipo de cambio | Política por defecto | Motivo |
|---|---|---|
| Stock | Auto-aplicar si la correlación es inequívoca. | Bajo riesgo; undo disponible. |
| Especificaciones | Auto-aplicar si la confianza ≥ 0.85. | Enriquecen la respuesta; no cambian el precio. |
| Estado (`ACTIVE`/`OUT_OF_STOCK`) | Auto-aplicar con stock coherente. | Evita ofrecer agotados. |
| Precio | Auto con límites y confianza alta; si no, revisión. | Alto impacto económico. |
| Zonas de entrega | Revisión. | Afecta la promesa de entrega. |
| Moneda | Revisión si no es USD/CUP. | El esquema no soporta MLC. |

Settings (migración `014`):

```text
catalog.autoapply.stock = 1
catalog.autoapply.specs = 1
catalog.autoapply.price = 1
catalog.price_max_delta_pct = 50
catalog.autoapply.min_confidence = 0.85
```

---

## 2. Triage del mensaje TCP

Hoy `ingest._ingest_supplier()` encola siempre `tcp_reply`, y `tcp_reply` asume que el
mensaje responde a una escalación. Eso no cubre:

- un ack de orden (`RECIBIDO`);
- una entrega (`ENTREGADO`);
- un rechazo (`AGOTADO`);
- una actualización proactiva (“bajé el precio”);
- una pregunta del TCP;
- un audio.

Se introduce `tcp_process` (reemplaza/absorbe `tcp_reply`):

```text
Mensaje TCP
  ├─ ¿audio? → transcribe_audio(next_job='tcp_process')
  ├─ triage:
  │    ├─ ESCALATION_ANSWER   (trae [E-###] o hay una sola escalación pendiente)
  │    ├─ ORDER_ACK           (trae [V-######] o hay una venta DISPATCHED)
  │    ├─ ORDER_DELIVERED     ("entregado", "cobrado")
  │    ├─ ORDER_REJECTED      ("agotado", "no hay", "no puedo")
  │    ├─ CATALOG_UPDATE      (precio/stock/specs sin caso)
  │    ├─ QUESTION            (pregunta al operador)
  │    └─ OTHER
  └─ handler correspondiente
```

### 2.1 Audio TCP: bug actual y corrección

`_ingest_supplier` guarda el audio y encola `tcp_reply`, pero `tcp_reply` lee
`message["body"]`, que es `None` para audio. **Hoy el audio del TCP no se transcribe.**

Corrección:

```python
# ai/ingest.py (proveedor)
job_type = "transcribe_audio" if type == "audio" else "tcp_process"
queue.enqueue(job_type, {
    "conversation_id": conversation["id"],
    "message_id": inbound["id"],
    "supplier_id": supplier["id"],
    "next_job": "tcp_process",
}, priority=config.PRIORITY_TCP)
```

`transcribe_audio` guarda el texto y, si `next_job == "tcp_process"`, encola el triage
en lugar de `generate_reply`.

### 2.2 Idempotencia

- `tcp_process` registra un evento `tcp_processed` con `message_id`; un job repetido
  no vuelve a aplicar la mutación.
- `catalog_mutation_log.source_message_id` es único por mensaje (índice parcial), para
  que un reintento no duplique la fila.

---

## 3. Correlación con `[E-###]` y `[V-######]`

### 3.1 Formato

No hace falta una columna nueva: `escalations.id` ya existe.

```text
Buenas colega, consulta sobre tu producto ref: [E-0104]
Producto: Batería Portátil 10000mAh
¿Tienes stock disponible y qué garantía das?
```

```python
CASE_ID_PATTERN = re.compile(r"\[?([EV])[-_ ]?(\d{1,6})\]?", re.IGNORECASE)
```

- `E-104` → escalación 104.
- `V-000042` → venta 42 (por `order_code`).
- Whisper puede transcribir `E-104` como “E 104” o “e104”; el regex lo cubre.
- Si el audio pierde el código, se usa la heurística de la sección 3.2.

### 3.2 Heurística sin código (corregida)

```text
1. Buscar escalaciones PENDING únicamente de ese supplier_id.
2. Si hay una sola: correlacionar (method='single_pending').
3. Si hay varias:
   a. Comparar la transcripción con el nombre del producto de cada escalación
      (léxica + embeddings).
   b. Si hay una coincidencia clara (score ≥ umbral y margen sobre la segunda):
      correlacionar (method='heuristic', confidence=score).
   c. Si hay ambigüedad: NO aplicar. Crear una fila en catalog_mutation_log con
      status='PENDING_REVIEW', ambiguous=1, y avisar al operador.
```

**No se usa “la más reciente”** como hacía la propuesta original: eso es exactamente
lo que corrompe el catálogo.

---

## 4. Extractor estructurado (`ai/tcp_extractor.py`)

Se usa el modelo local con salida estructurada (`json_schema`), no texto libre.

```python
from pydantic import BaseModel, Field
from typing import Literal

class TCPUpdateExtraction(BaseModel):
    has_catalog_update: bool
    stock: int | None = Field(default=None, ge=0, le=100_000)
    price: float | None = Field(default=None, gt=0)
    currency: Literal["USD", "CUP"] | None = None
    status: Literal["ACTIVE", "OUT_OF_STOCK", "PAUSED"] | None = None
    new_specifications: dict[str, str | bool | int | float] = Field(default_factory=dict)
    delivery_zone_names: list[str] = Field(default_factory=list)
    customer_answer_summary: str = ""
    confidence: float = Field(ge=0.0, le=1.0)
    notes: str = ""
```

Correcciones:

- **No se permite `MLC`**: `products.currency` solo acepta `USD`/`CUP`. Si el TCP dice
  “MLC”, el extractor debe mapearlo a `USD` solo si la política del negocio lo define;
  si no, `notes` lo indica y va a revisión.
- `new_specifications` es un diccionario de valores simples; no se aceptan objetos
  anidados arbitrarios.
- `customer_answer_summary` se usa solo para la respuesta al cliente; el backend puede
  reemplazarla por una plantilla si el guardrail falla.
- No se pide “razonamiento paso a paso”; gasta tokens y no es auditable.

### 4.1 Prompt del extractor

```text
Eres el auditor de catálogo de Cuadra. Extrae datos comerciales precisos de la
respuesta de un proveedor (TCP) en Cuba.

Producto:
- ID: {{ product.id }}
- Nombre: {{ product.name }}
- Precio actual: {{ product.price }} {{ product.currency }}
- Stock físico: {{ product.stock }}
- Reservado: {{ product.stock_reserved }}
- Especificaciones: {{ product.specifications_json }}

Mensaje del TCP (texto o transcripción):
"{{ raw_message }}"

Reglas:
1. "fula", "USD", "dólares" → USD. "CUP", "moneda nacional", "pesos" → CUP.
2. "no me queda", "se me fue", "agotado" → stock=0, status=OUT_OF_STOCK.
3. Si da un plazo de garantía, agrégalo en new_specifications["garantia"].
4. Si no estás seguro de un dato, déjalo en null y baja la confianza.
5. No inventes precios, stock ni zonas.
6. Responde solo con el JSON del esquema.
```

---

## 5. Mutación atómica (`domain/catalog_mutations.py`)

### 5.1 Esquema `catalog_mutation_log` (migración `015`)

```sql
CREATE TABLE IF NOT EXISTS catalog_mutation_log (
    id                  INTEGER PRIMARY KEY,
    product_id          INTEGER NOT NULL REFERENCES products(id),
    supplier_id         INTEGER NOT NULL REFERENCES suppliers(id),
    escalation_id       INTEGER REFERENCES escalations(id),
    sale_id             INTEGER REFERENCES sales(id),
    source_message_id   INTEGER REFERENCES messages(id),
    source_text         TEXT,
    mutation_type       TEXT NOT NULL,   -- PRICE | STOCK | STATUS | SPECS | ZONE | MIXED
    previous_state_json TEXT NOT NULL,
    new_state_json      TEXT NOT NULL,
    previous_updated_at TEXT,
    confidence          REAL NOT NULL DEFAULT 0.0,
    correlation_method  TEXT NOT NULL DEFAULT 'manual',  -- case_id | single_pending | heuristic | manual
    ambiguous           INTEGER NOT NULL DEFAULT 0 CHECK (ambiguous IN (0, 1)),
    status              TEXT NOT NULL DEFAULT 'APPLIED'
                        CHECK (status IN ('APPLIED', 'PENDING_REVIEW', 'REVERTED', 'REJECTED')),
    applied_by          TEXT NOT NULL DEFAULT 'bot' CHECK (applied_by IN ('bot', 'operator')),
    reverted_at         TEXT,
    reverted_by         TEXT,
    revert_reason       TEXT,
    created_at          TEXT DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_mutation_source
    ON catalog_mutation_log(source_message_id)
    WHERE source_message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_mutation_status
    ON catalog_mutation_log(status, created_at DESC);
```

`previous_state_json` / `new_state_json` incluyen:

```json
{
  "price": 25.0,
  "currency": "USD",
  "stock": 5,
  "stock_reserved": 1,
  "status": "ACTIVE",
  "specifications": {"garantia": "6 meses"},
  "delivery_zone_ids": [1, 13]
}
```

### 5.2 Aplicar o proponer

```python
def apply_tcp_update(conn, *, product_id, supplier_id, escalation_id,
                     source_message_id, source_text, extraction):
    product = catalog.get_product(conn, product_id)
    previous = snapshot(product)  # incluye updated_at

    # 1. Validar rangos y moneda
    validate_extraction(extraction, product)

    # 2. Decidir política
    decision = mutation_policy(extraction, previous)  # APPLIED | PENDING_REVIEW

    # 3. Construir nuevo estado
    new_state = merge_state(previous, extraction)

    # 4. Transacción atómica (sin BEGIN manual)
    with conn:
        if decision == "APPLIED":
            conn.execute(
                """UPDATE products
                   SET price = ?, currency = ?, stock = ?, status = ?,
                       specifications_json = ?, updated_at = datetime('now')
                   WHERE id = ?""",
                (new_state["price"], new_state["currency"], new_state["stock"],
                 new_state["status"], json.dumps(new_state["specifications"]),
                 product_id),
            )
        conn.execute(
            """INSERT INTO catalog_mutation_log (
                   product_id, supplier_id, escalation_id, source_message_id,
                   source_text, mutation_type, previous_state_json, new_state_json,
                   previous_updated_at, confidence, correlation_method, ambiguous, status
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (...),
        )

    # 5. FTS se actualiza por trigger; embeddings se reindexan por job
    if decision == "APPLIED":
        queue.enqueue("reindex_product", {"product_id": product_id}, priority=90)

    return log_id
```

> Si la mutación incluye `ZONE`, la misma transacción reemplaza las filas de
> `supplier_delivery_zones` del TCP y guarda `delivery_zone_ids` en el snapshot. El
> revert debe restaurar también esas filas, no solo las columnas de `products`.

### 5.3 Validaciones obligatorias

| Campo | Regla |
|---|---|
| `price` | `> 0`; si `autoapply.price=0`, siempre `PENDING_REVIEW`. Si auto, cambio dentro de `price_max_delta_pct`; si no, revisión. |
| `currency` | Solo `USD`/`CUP`; si no, revisión. |
| `stock` | `0..100000`; entero. |
| `status` | `ACTIVE`, `OUT_OF_STOCK`, `PAUSED`; no `REMOVED`. |
| `specs` | Claves/valores simples; no sobrescribir con vacío. |
| `delivery_zone_names` | Deben existir en `zones`/`zone_aliases`; si no, revisión. |
| Coherencia | `stock=0` → `OUT_OF_STOCK`; `stock>0` y `status=OUT_OF_STOCK` → `ACTIVE` (solo si no estaba `PAUSED`). |

### 5.4 Stock físico vs. reservado

El TCP manda **stock físico absoluto** (“me quedan 2”). No se debe restar lo reservado
del valor que él dice. La disponibilidad se calcula:

```text
available = stock - stock_reserved
```

Si `stock_reserved > stock`, hay sobreventa: se marca `NEEDS_OPERATOR`, se avisa y no
se ofrecen más unidades de ese producto.

### 5.5 Resolución de la escalación y reanudación

Si la mutación viene de una escalación:

1. `escalations.set_answer(...)` (ANSWERED).
2. Aplicar/proponer mutación.
3. Si se aplicó: `escalations.resolve(...)` (RESOLVED) y responder al cliente con una
   plantilla que use el dato verificado.
4. Si quedó `PENDING_REVIEW`: la escalación sigue `ANSWERED`; el operador decide.
5. Si hay **varios clientes** esperando el mismo producto, se buscan sus escalaciones
   abiertas y se reanudan una por una (no solo la conversación original).

---

## 6. Undo con detección de conflicto

La propuesta original restauraba `previous_state_json` sin comprobar si el producto
había cambiado después. Si hay dos mutaciones seguidas, revertir la primera pisa la
segunda.

Regla corregida:

```text
1. Cargar log #id y el producto actual.
2. Comparar el estado actual con `new_state_json` de esa mutación.
3. Si no coincide → 409 "el producto cambió después de esta mutación".
4. Si coincide → restaurar `previous_state_json` y marcar REVERTED.
```

### 6.1 Endpoint

`_run` es el helper de `02` §7: abre `db.connect()`, ejecuta y cierra. El router usa
`Depends(require_pin)`.

```python
router = APIRouter(prefix="/api", dependencies=[Depends(require_pin)])

@router.post("/catalog/mutations/{mutation_id}/revert")
async def revert_mutation(mutation_id: int, body: RevertIn):
    def _revert(conn):
        log = conn.execute(
            "SELECT * FROM catalog_mutation_log WHERE id = ?", (mutation_id,)
        ).fetchone()
        if log is None:
            raise HTTPException(404, "mutación no encontrada")
        if log["status"] != "APPLIED":
            raise HTTPException(409, f"la mutación está en estado {log['status']}")

        product = catalog.get_product(conn, log["product_id"])
        current = snapshot(product)
        expected = json.loads(log["new_state_json"])
        if not states_match(current, expected):
            raise HTTPException(
                409,
                "el producto cambió después de esta mutación; revierte la más reciente",
            )

        previous = json.loads(log["previous_state_json"])
        with conn:
            conn.execute(
                """UPDATE products
                   SET price = ?, currency = ?, stock = ?, status = ?,
                       specifications_json = ?, updated_at = datetime('now')
                   WHERE id = ?""",
                (previous["price"], previous["currency"], previous["stock"],
                 previous["status"], json.dumps(previous["specifications"]),
                 log["product_id"]),
            )
            conn.execute(
                """UPDATE catalog_mutation_log
                   SET status = 'REVERTED', reverted_at = datetime('now'),
                       reverted_by = 'operator', revert_reason = ?
                   WHERE id = ?""",
                (body.reason, mutation_id),
            )
            events.log(
                conn, "dashboard", "catalog_mutation_reverted",
                entity="products", entity_id=log["product_id"],
                details={"mutation_id": mutation_id, "reason": body.reason},
            )
        queue.enqueue("reindex_product", {"product_id": log["product_id"]}, priority=90)
        return catalog.get_product(conn, log["product_id"])
    return _run(_revert)
```

### 6.2 Mutaciones en revisión

- `PENDING_REVIEW` no se aplica; el operador puede:
  - `POST /api/catalog/mutations/{id}/approve` → aplica y marca `APPLIED`.
  - `POST /api/catalog/mutations/{id}/reject` → marca `REJECTED`.
- Aprobar una mutación en revisión también debe detectar conflicto: si el producto
  cambió desde que se propuso, se recalcula o se rechaza.

---

## 7. Panel de auditoría

Nuevo panel `dashboard/src/panels/audit/`:

```text
┌────────────────────────────────────────────────────────────┐
│ ACTUALIZACIÓN DE CATÁLOGO — Log #104                       │
│ Producto: Power Bank 10000mAh (ID: 42)                     │
│ Proveedor: Almacén Playa                                   │
│ Origen: Audio de Telegram (14 s)                           │
│ Transcripción: "Asere sí, me quedan 2 y el precio subió a 30"│
├────────────────────────────────────────────────────────────┤
│ Cambios:                                                   │
│ • Precio: 25.00 USD → 30.00 USD  (revisión: +20%)          │
│ • Stock: 5 → 2                                              │
├────────────────────────────────────────────────────────────┤
│ Estado: [PENDIENTE DE REVISIÓN]  [Aprobar] [Rechazar]      │
│ Si ya aplicado:                 [Revertir cambio]          │
└────────────────────────────────────────────────────────────┘
```

Filtros: estado, TCP, producto, fecha, `ambiguous`.
Acciones: aprobar, rechazar, revertir; ver mensaje original y snapshot.

---

## 8. Qué NO se hace

- No se escribe SQL desde el LLM.
- No se auto-aplica un precio si la correlación es ambigua.
- No se usa “la escalación más antigua” ni “la más reciente” como fallback.
- No se acepta `MLC` en `products.currency`.
- No se sobrescribe `specifications_json` completo; se fusiona.
- No se revierte a ciegas si el producto cambió después.
- No se reenvía la respuesta cruda del TCP al cliente; se usa una plantilla/guardrail.

---

## 9. Criterios de aceptación

| Caso | Resultado |
|---|---|
| TCP responde `[E-0104]` con stock 2 | Escalación 104 resuelta; stock=2; log APPLIED; cliente reanudado. |
| TCP manda audio sin código, una sola escalación pendiente | Se transcribe, se correlaciona por `single_pending`, se aplica. |
| TCP manda audio ambiguo con 3 escalaciones | No se aplica; log `PENDING_REVIEW`, `ambiguous=1`; operador avisado. |
| TCP dice “precio 28” (antes 30, −6.7%) con confianza 0.9 | Dentro de ±50%; `APPLIED` con log y undo. |
| TCP dice “precio 300” con autoapply.price=1 y +50% | Fuera de rango; `PENDING_REVIEW`. |
| TCP dice un precio con confianza 0.5 | Bajo el mínimo; `PENDING_REVIEW`. |
| Operador revierte el último cambio | Producto vuelve al estado anterior; log `REVERTED`. |
| Operador revierte un cambio viejo | `409`; no se corrompe el producto. |
| Dos clientes esperando el mismo producto | Ambos reciben la respuesta; no se duplica la mutación. |
| Reintento del job TCP | No se duplica la fila ni la mutación (`source_message_id` único). |
