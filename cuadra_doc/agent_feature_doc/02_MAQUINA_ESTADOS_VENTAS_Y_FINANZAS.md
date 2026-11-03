---
tipo: especificacion_tecnica
proyecto: Cuadra
modulo: ventas_y_finanzas
estado: DISEÑO APROBADO — listo para implementar
revisado: 2026-11-03
aprobado: 2026-11-03
revision: 2
tags: [ventas, maquina-estados, comisiones, tcp-dispatch, ledger, finanzas]
---

# 02 — Venta, despacho al TCP y control de comisiones

> **Cambios de esta revisión:** se unificaron los estados, se reconstruye `sales`
> (SQLite no permite cambiar el `CHECK` con `ALTER TABLE`), se añadió
> `stock_reserved`, `commission_currency` y `delivery_zone_id`, se corrigió el insert
> que omitía `supplier_id` y usaba `source='telegram'`, y la API usa el patrón real
> `db.connect()` + `require_pin`.

---

## 1. Rol del agente y del operador

El agente asume la fricción transaccional; el operador no redacta mensajes rutinarios.
Pero **el pago lo cierra el operador** y, en modo asistido, también aprueba la venta.

| Responsabilidad | Agente | Operador |
|---|---|---|
| Detectar intención de compra | Sí | — |
| Extraer nombre, zona, dirección, teléfono | Sí | — |
| Validar zona contra catálogo y cobertura del TCP | Sí | — |
| Crear venta y reservar stock | Sí | — |
| Aprobar venta (modo asistido) | — | Sí, un clic |
| Despachar orden al TCP | Sí | — |
| Confirmar entrega/cobro al cliente | Sí (con datos del TCP) | — |
| Cobrar la comisión al TCP | — | Sí |
| Revertir mutación de catálogo | — | Sí |
| Cancelar una venta | Solo antes de despachar | Sí, en cualquier momento |

---

## 2. Estados (vocabulario único)

### 2.1 Etapa de conversación (`conversations.context_json.stage`)

```text
DISCOVERY → PRODUCT_MATCH → QUOTE → OBJECTION → CLOSE
   → CAPTURE_DATA → CONFIRM → DONE
   ↘ HANDOFF
```

### 2.2 Estado de la venta (`sales.status`)

```text
DRAFT
  └─► PENDING_APPROVAL ──(operador aprueba)──► PENDING_DISPATCH
  └─► PENDING_DISPATCH ──(modo autónomo)─────► PENDING_DISPATCH
                                                    │
                                                    ▼ send ok
                                               DISPATCHED
                                                    │ TCP acepta
                                                    ▼
                                               CONFIRMED
                                                    │ TCP entrega y cobra
                                                    ▼
                                               DELIVERED
                                                    │ operador cobra comisión
                                                    ▼
                                               SETTLED

Excepciones:
  CANCELLED        antes de entregar (stock reservado se libera)
  NEEDS_OPERATOR   rechazo del TCP, ambigüedad o error de despacho
```

**No se usa `PAID` para la comisión.** `PAID` se migra a `SETTLED`. El cliente paga
al TCP en la entrega (`DELIVERED`); Cuadra cobra su comisión después (`SETTLED`).

### 2.3 Ledger financiero (`commission_ledger.status`)

```text
PENDING → COLLECTED
   └────► VOID (venta cancelada)
```

El ledger **no** duplica `DISPATCHED/CONFIRMED/DELIVERED`; eso vive en `sales`.

---

## 3. Validación de slots

### 3.1 Zonas: catálogo CRUD, no diccionario hardcodeado

La propuesta original traía un `HABANA_ZONES` hardcodeado con “16 municipios”. La
decisión aprobada (D3) es usar el CRUD real:

- `zones` es la **fuente de verdad** y el operador la administra (crear, editar,
  desactivar). Cualquier zona que exista y esté activa es válida.
- **No hay lista fija de municipios** ni se asume La Habana: las 10 filas actuales son
  datos de prueba/demo y pueden cambiar.
- Las zonas **no se borran**: se desactivan con `active = 0`, igual que los grupos.
- El nombre real en la BD manda (`San Miguel`, no `San Miguel del Padrón`); si se quiere
  aceptar el nombre largo, se añade un alias.
- La cobertura de cada TCP está en `supplier_delivery_zones` (N:M).

Por eso (la tabla se crea en `013_memory_retrieval.sql`, porque la Fase 2 valida slots
antes de que exista el flujo de ventas):

```sql
CREATE TABLE IF NOT EXISTS zone_aliases (
    alias   TEXT PRIMARY KEY,              -- normalizado: minúsculas, sin acentos
    zone_id INTEGER NOT NULL REFERENCES zones(id) ON DELETE CASCADE
);
```

- `alias` se guarda normalizado (`_fold`): `vedado`, `alamar`, `san miguel`, etc.
- Si el cliente escribe una zona que no está en `zones` ni en `zone_aliases`, el bot
  pide elegir de la lista activa. No adivina.
- Si la zona existe pero el TCP del producto no la cubre, se ofrecen alternativas de
  TCPs que sí la cubran o se escala al operador.
- El agente nunca dice “solo entregamos en los 15 municipios de La Habana”; dice las
  zonas activas que devuelve el catálogo.

### 3.2 Teléfono

Regla corregida: aceptar **6–8 dígitos**, con `+53` opcional, espacios/guiones.
No exigir que empiece en 5 o 6: hay fijos que empiezan en 7.

```python
def normalize_phone(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("53"):
        digits = digits[2:]
    if not (6 <= len(digits) <= 8):
        return None
    return f"+53 {digits}"
```

### 3.3 Nombre y dirección

- `recipient_name`: mínimo 2 caracteres; rechazar “yo”, “mí”, “el mismo”.
- `delivery_address`: no exigir un formato rígido; pedir calle, número y entrecalles
  o una referencia clara. Validar longitud y que no sea solo “mi casa”.
- Si el cliente da la dirección en dos mensajes, el turno consolidado y los hechos
  fijados la reconstruyen; no se pierde por el debounce.

### 3.4 Cobertura y disponibilidad

Antes de crear la venta:

1. Producto existe y `status='ACTIVE'`.
2. `supplier.active = 1`.
3. `stock - stock_reserved >= quantity`.
4. La zona está en `supplier_delivery_zones` del TCP del producto.
5. Precio y moneda vigentes; snapshot en la venta.

Si algo falla: no se crea la venta; se ofrece alternativa o se escala.

---

## 4. Migración `014_sales_and_finance.sql`

> **Importante:** SQLite no permite `ALTER TABLE ... ADD COLUMN ... UNIQUE` ni cambiar
> un `CHECK`. Por eso `sales` se reconstruye con el mismo patrón de `006` y `011`:
> `PRAGMA foreign_keys=OFF`, crear tabla nueva, copiar, borrar, renombrar.

```sql
-- =========================================================
-- 014: ventas autónomas, stock reservado y ledger de comisiones
-- =========================================================

-- 1. Stock físico vs. reservado, y tipo de comisión del producto
ALTER TABLE products ADD COLUMN stock_reserved INTEGER NOT NULL DEFAULT 0;
ALTER TABLE products ADD COLUMN commission_type TEXT NOT NULL DEFAULT 'FIXED'
    CHECK (commission_type IN ('FIXED', 'PERCENT'));

-- 2. Reconstrucción de sales (nuevos estados y columnas)
PRAGMA foreign_keys = OFF;
BEGIN;

CREATE TABLE sales_new (
    id                  INTEGER PRIMARY KEY,
    order_code          TEXT,
    conversation_id     INTEGER REFERENCES conversations(id),
    product_id          INTEGER NOT NULL REFERENCES products(id),
    supplier_id         INTEGER NOT NULL REFERENCES suppliers(id),
    customer_id         INTEGER NOT NULL REFERENCES customers(id),
    quantity            INTEGER NOT NULL DEFAULT 1 CHECK (quantity > 0),
    amount              REAL NOT NULL CHECK (amount >= 0),
    currency            TEXT NOT NULL CHECK (currency IN ('USD', 'CUP')),
    commission_type     TEXT NOT NULL DEFAULT 'FIXED'
                        CHECK (commission_type IN ('FIXED', 'PERCENT')),
    commission          REAL NOT NULL,
    commission_currency TEXT NOT NULL DEFAULT 'USD'
                        CHECK (commission_currency IN ('USD', 'CUP')),
    recipient_name      TEXT,
    delivery_zone       TEXT,
    delivery_zone_id    INTEGER REFERENCES zones(id),
    delivery_address    TEXT,
    customer_phone      TEXT,
    status              TEXT NOT NULL DEFAULT 'DRAFT'
                        CHECK (status IN (
                            'DRAFT', 'PENDING_APPROVAL', 'PENDING_DISPATCH',
                            'DISPATCHED', 'CONFIRMED', 'DELIVERED',
                            'SETTLED', 'CANCELLED', 'NEEDS_OPERATOR'
                        )),
    source              TEXT NOT NULL DEFAULT 'bot'
                        CHECK (source IN ('bot', 'operator')),
    context_json        TEXT NOT NULL DEFAULT '{}',
    notes               TEXT,
    approved_at         TEXT,
    dispatched_at       TEXT,
    tcp_ack_at          TEXT,
    delivered_at        TEXT,
    settled_at          TEXT,
    cancelled_at        TEXT,
    created_at          TEXT DEFAULT (datetime('now')),
    updated_at          TEXT DEFAULT (datetime('now'))
);

INSERT INTO sales_new (
    id, conversation_id, product_id, supplier_id, customer_id,
    amount, currency, commission_type, commission, context_json,
    status, source, approved_at, settled_at, created_at, updated_at
)
SELECT
    id, conversation_id, product_id, supplier_id, customer_id,
    amount, currency, commission_type, commission, context_json,
    CASE status WHEN 'PAID' THEN 'SETTLED' ELSE status END,
    source, approved_at, paid_at, created_at, updated_at
FROM sales;

DROP TABLE sales;
ALTER TABLE sales_new RENAME TO sales;

CREATE UNIQUE INDEX IF NOT EXISTS idx_sales_order_code
    ON sales(order_code) WHERE order_code IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sales_status ON sales(status);
CREATE INDEX IF NOT EXISTS idx_sales_supplier ON sales(supplier_id);
CREATE INDEX IF NOT EXISTS idx_sales_customer ON sales(customer_id);

COMMIT;
PRAGMA foreign_keys = ON;

-- 3. Ledger financiero (sin duplicar el ciclo de entrega)
CREATE TABLE IF NOT EXISTS commission_ledger (
    id                  INTEGER PRIMARY KEY,
    sale_id             INTEGER NOT NULL UNIQUE REFERENCES sales(id),
    product_id          INTEGER NOT NULL REFERENCES products(id),
    supplier_id         INTEGER NOT NULL REFERENCES suppliers(id),
    product_name        TEXT NOT NULL,
    sale_total          REAL NOT NULL,
    currency            TEXT NOT NULL CHECK (currency IN ('USD', 'CUP')),
    commission_type     TEXT NOT NULL CHECK (commission_type IN ('FIXED', 'PERCENT')),
    commission_rate     REAL NOT NULL,
    commission_earned   REAL NOT NULL,
    commission_currency TEXT NOT NULL CHECK (commission_currency IN ('USD', 'CUP')),
    supplier_payout     REAL,   -- NULL si las monedas no coinciden
    status              TEXT NOT NULL DEFAULT 'PENDING'
                        CHECK (status IN ('PENDING', 'COLLECTED', 'VOID')),
    collected_at        TEXT,
    notes               TEXT,
    created_at          TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ledger_status ON commission_ledger(status);

-- 4. Configuración de autonomía y operador
INSERT OR IGNORE INTO settings (key, value) VALUES
    ('sales.mode', 'assisted'),
    ('sales.hybrid_max_age_hours', '24'),
    ('catalog.autoapply.price', '1'),
    ('catalog.autoapply.stock', '1'),
    ('catalog.autoapply.specs', '1'),
    ('catalog.price_max_delta_pct', '50'),
    ('catalog.autoapply.min_confidence', '0.85');
```

Notas:

- `settings` ya existe (migración `002`).
- `zone_aliases` se crea en `013_memory_retrieval.sql`, no aquí.
- `commission_ledger.commission_rate` guarda el valor configurado: monto por unidad si
  `FIXED`, porcentaje si `PERCENT`. El monto ganado va en `commission_earned`.
- `paid_at` se copia a `settled_at`; no se pierde historia.
- `order_code` no es `UNIQUE` en la columna, sino por índice parcial. SQLite permite
  varios `NULL`.
- No se añade `items_json`; una venta es de un producto. Para combos, en el futuro,
  se crea `sale_items` con `sale_id`, `product_id`, `quantity`, `unit_price`.

---

## 5. Despacho al TCP

### 5.1 Código de orden

No se usan 4 dígitos aleatorios. Se inserta la venta y luego:

```sql
UPDATE sales
SET order_code = 'V-' || printf('%06d', id)
WHERE id = ?;
```

El código es estable, único y legible (`V-000042`).

### 5.2 Mensaje al TCP

```text
📦 NUEVA ORDEN — Ref: V-000042

Producto: Batería Portátil 10000mAh (ID: 42)
Cantidad: 1
Total a cobrar al cliente: 25.00 USD (cobro en destino)

Datos de entrega:
• Cliente: Carlos Martínez
• Municipio: Marianao
• Dirección: Calle 100 #2904 e/ 29 y 31
• Teléfono: +53 52345678

Responde a este mensaje con:
• RECIBIDO  → confirmas que la preparas
• AGOTADO   → no tienes disponibilidad
• ENTREGADO → ya la entregaste y cobraste
```

### 5.3 Mensaje al cliente

No se promete entrega antes de que el TCP confirme:

- Al crear la venta: “Tu pedido **V-000042** quedó registrado. El almacén confirmará
  la entrega; el pago es en efectivo al recibir.”
- Cuando el TCP responde `RECIBIDO`: “Confirmado, tu pedido está en preparación.”
- Cuando el TCP responde `ENTREGADO`: “Entregado. ¡Gracias por confiar en Cuadra!”
- Si el TCP responde `AGOTADO`: mensaje honesto + alternativa o devolución de la
  reserva.

### 5.4 Idempotencia del despacho

- Estado `PENDING_DISPATCH` → `DISPATCHED` solo si el envío fue exitoso.
- Evento `sale_dispatched` con `sale_id`; un job repetido no reenvía.
- `tcp_telegram_message_id` se guarda si el adaptador devuelve el id.
- Si el adaptador no devuelve id, se guarda `NULL` y se usa el evento como prueba.

> **Cambio de contrato necesario:** `channels/telegram.py` y
> `channels/telegram_user.py` deben devolver el id del mensaje enviado. Hoy
> `send_text` devuelve `None`. Sin eso no se puede correlacionar un ack por reply.

---

## 6. Dominio de ventas (`domain/sales.py`)

### 6.1 Cálculo de comisión

`products.commission_type` determina cómo se interpreta `products.commission`:

- `FIXED`: monto por unidad, en `products.commission_currency`.
- `PERCENT`: porcentaje del total, en la misma moneda de la venta.

```python
def calculate_commission(product, quantity, sale_currency):
    total = product["price"] * quantity
    ctype = product.get("commission_type") or "FIXED"
    rate = product["commission"] or 0.0
    ccurrency = product["commission_currency"] or "USD"

    if ctype == "PERCENT":
        earned = total * (rate / 100.0)
        ccurrency = sale_currency  # un porcentaje no tiene moneda propia
    else:
        earned = rate * quantity

    payout = total - earned if ccurrency == sale_currency else None
    return earned, payout, total, ccurrency, ctype
```

Si `ccurrency != sale_currency`, `supplier_payout` queda `NULL` y el dashboard muestra
“requiere conversión”. **No se resta USD de CUP.**

### 6.2 Crear venta (reserva atómica)

En modo `hybrid`, la venta se crea en `PENDING_DISPATCH` solo si se cumplen **todas**:

- `stock - stock_reserved >= quantity`;
- la zona está cubierta por el TCP;
- el producto no tiene una escalación pendiente;
- `products.updated_at` es reciente (setting `sales.hybrid_max_age_hours`, default 24).

Si falla una, se crea en `PENDING_APPROVAL`. En modo `autonomous` siempre va a
`PENDING_DISPATCH`; en `assisted`, siempre a `PENDING_APPROVAL`.

```python
def create_sale(conn, *, conversation_id, customer_id, product_id,
                quantity, slots, mode):
    product = catalog.get_product(conn, product_id)
    if product["status"] != "ACTIVE":
        raise Invalid("product is not sellable")
    supplier = catalog.get_supplier(conn, product["supplier_id"])
    if not supplier["active"]:
        raise Invalid("supplier is inactive")
    if quantity <= 0:
        raise Invalid("quantity must be positive")

    available = (product["stock"] or 0) - (product["stock_reserved"] or 0)
    if available < quantity:
        raise Invalid("not enough stock")

    zone = resolve_zone(conn, slots.delivery_zone)  # zones + zone_aliases
    if zone is None:
        raise Invalid("unknown zone")
    if not supplier_covers_zone(conn, supplier["id"], zone["id"]):
        raise Invalid("supplier does not deliver to this zone")

    earned, payout, total, ccurrency, ctype = calculate_commission(
        product, quantity, product["currency"]
    )
    initial_status = sale_initial_status(conn, mode, product, zone, quantity, supplier)

    with conn:  # transacción atómica; NO usar BEGIN manual
        cur = conn.execute(
            """INSERT INTO sales (
                   conversation_id, product_id, supplier_id, customer_id,
                   quantity, amount, currency, commission_type, commission,
                   commission_currency, recipient_name, delivery_zone,
                   delivery_zone_id, delivery_address, customer_phone,
                   status, source, context_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'bot', ?)""",
            (
                conversation_id, product_id, product["supplier_id"], customer_id,
                quantity, total, product["currency"], ctype,
                earned, ccurrency, slots.recipient_name, zone["name"], zone["id"],
                slots.delivery_address, slots.customer_phone,
                initial_status,
                json.dumps({"product_name": product["name"]}, ensure_ascii=False),
            ),
        )
        sale_id = cur.lastrowid

        conn.execute(
            "UPDATE sales SET order_code = 'V-' || printf('%06d', id) WHERE id = ?",
            (sale_id,),
        )
        conn.execute(
            """INSERT INTO commission_ledger (
                   sale_id, product_id, supplier_id, product_name,
                   sale_total, currency, commission_type, commission_rate,
                   commission_earned, commission_currency, supplier_payout
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                sale_id, product_id, product["supplier_id"], product["name"],
                total, product["currency"], ctype,
                product["commission"], earned, ccurrency, payout,
            ),
        )
        conn.execute(
            "UPDATE products SET stock_reserved = stock_reserved + ?, updated_at = ? WHERE id = ?",
            (quantity, now(), product_id),
        )

    return get_sale(conn, sale_id)
```

Puntos clave:

- El insert incluye `supplier_id`, `conversation_id` y `source='bot'`; el original los
  omitía y usaba `'telegram'`, que viola el `CHECK`.
- La reserva se hace con `stock_reserved`, no descontando `stock` físico.
- `with conn:` hace commit/rollback; no se usa `BEGIN TRANSACTION` manual.
- `order_code` se genera después del insert.
- Si falla la inserción del ledger, la venta y la reserva se revierten juntas.

### 6.3 Transiciones

| Acción | Desde | Hacia | Efecto en stock |
|---|---|---|---|
| Aprobar | `PENDING_APPROVAL` | `PENDING_DISPATCH` | — |
| Despachar | `PENDING_DISPATCH` | `DISPATCHED` | — |
| TCP acepta | `DISPATCHED` | `CONFIRMED` | — |
| TCP entrega | `CONFIRMED` | `DELIVERED` | `stock -= qty`; `stock_reserved -= qty` |
| Operador cobra | `DELIVERED` | `SETTLED` | Ledger `COLLECTED` |
| Cancelar | antes de `DELIVERED` | `CANCELLED` | `stock_reserved -= qty` |
| Rechazo TCP | `DISPATCHED` | `NEEDS_OPERATOR` | Reserva se mantiene hasta decisión |
| Error de despacho | `PENDING_DISPATCH` | `NEEDS_OPERATOR` | Reserva se mantiene |

Reglas:

- `stock_reserved` nunca baja de 0.
- Al entregar, si `stock < qty`, no se permite: se marca `NEEDS_OPERATOR` y se avisa.
- Cancelar una venta `DELIVERED` no devuelve stock físico; requiere decisión del
  operador y una nota.

---

## 7. API de finanzas

Patrón real del proyecto:

```python
router = APIRouter(prefix="/api", dependencies=[Depends(require_pin)])

def _run(fn, *args, **kwargs):
    conn = db.connect()
    try:
        return fn(conn, *args, **kwargs)
    finally:
        conn.close()
```

No existe `Depends(get_db)`. El PIN va en `X-PIN`.

### 7.1 `GET /api/finance/summary`

```json
{
  "mode": "assisted",
  "sales_today": 5,
  "sales_pending_approval": 1,
  "sales_dispatched": 2,
  "sales_delivered_pending_settlement": 2,
  "totals_by_currency": [
    {
      "currency": "USD",
      "gross_sales": 710.0,
      "commission_earned": 95.0,
      "commission_collected": 75.0,
      "commission_pending": 20.0
    },
    {
      "currency": "CUP",
      "gross_sales": 12000.0,
      "commission_earned": 900.0,
      "commission_collected": 0.0,
      "commission_pending": 900.0
    }
  ]
}
```

**No se devuelve un único `ganancia_total_usd`** si hay CUP: sumar monedas distintas
es un error contable.

### 7.2 `GET /api/finance/ledger`

Lista paginada con filtros `status`, `currency`, `supplier_id`, `from`, `to`.

### 7.3 `POST /api/finance/settle/{sale_id}`

```python
@router.post("/finance/settle/{sale_id}")
async def settle_commission(sale_id: int):
    def _settle(conn):
        with conn:
            sale = conn.execute("SELECT * FROM sales WHERE id = ?", (sale_id,)).fetchone()
            if sale is None:
                raise HTTPException(404, "venta no encontrada")
            if sale["status"] != "DELIVERED":
                raise HTTPException(409, "la venta no está entregada")
            conn.execute(
                """UPDATE commission_ledger
                   SET status = 'COLLECTED', collected_at = datetime('now')
                   WHERE sale_id = ? AND status = 'PENDING'""",
                (sale_id,),
            )
            conn.execute(
                "UPDATE sales SET status = 'SETTLED', settled_at = datetime('now'), updated_at = datetime('now') WHERE id = ?",
                (sale_id,),
            )
            events.log(conn, "dashboard", "commission_settled",
                       entity="sales", entity_id=sale_id)
        return get_sale(conn, sale_id)
    return _run(_settle)
```

### 7.4 `POST /api/finance/cancel/{sale_id}`

- Solo si `status` está antes de `DELIVERED`.
- Libera `stock_reserved` (nunca por debajo de 0).
- Ledger a `VOID`.
- Evento `sale_cancelled` con motivo.
- Si ya se despachó, se notifica al TCP (si es posible) y al operador.

### 7.5 `POST /api/finance/approve/{sale_id}`

- Solo en modo `assisted` y estado `PENDING_APPROVAL`.
- Pasa a `PENDING_DISPATCH` y encola `dispatch_sale`.
- Evento `sale_approved`.

---

## 8. Dashboard

Nuevo panel `dashboard/src/panels/finance/`:

- Tarjetas: ventas hoy, pendientes de aprobar, despachadas, entregadas por liquidar.
- Totales **por moneda**.
- Tabla de ventas con estado, producto, TCP, zona, comisión y acciones:
  `[Aprobar]`, `[Cancelar]`, `[Marcar comisión cobrada]`.
- Filtro por estado y moneda.
- Enlace a la conversación (si existe el panel de conversaciones; hoy no existe).

Rutas:

```tsx
<Route path="finance" element={<FinancePanel />} />
```

Tipos nuevos en `dashboard/src/types/api.ts` y funciones en `lib/api.ts`:

```ts
export const getFinanceSummary = () => request<FinanceSummary>("/api/finance/summary");
export const listLedger = (params?) => request<LedgerPage>("/api/finance/ledger");
export const approveSale = (id: number) => request<Sale>(`/api/finance/approve/${id}`, { method: "POST" });
export const settleSale = (id: number) => request<Sale>(`/api/finance/settle/${id}`, { method: "POST" });
export const cancelSale = (id: number, reason: string) =>
  request<Sale>(`/api/finance/cancel/${id}`, { method: "POST", body: JSON.stringify({ reason }) });
```

---

## 9. Notificación al operador

**Decisión aprobada (D6-A): solo dashboard.** No se envía nada al Telegram del
operador ni se añade un setting `sales.operator_telegram_id`.

Las alertas se muestran en:

- **Finanzas:** ventas `PENDING_APPROVAL`, `NEEDS_OPERATOR`, `DELIVERED` por liquidar.
- **Auditoría:** mutaciones `PENDING_REVIEW`, conflictos de undo, sobreventa.
- **Logs:** eventos `sale_*`, `catalog_mutation_*`, `guardrail_violation`.

No se usa el mismo chat del cliente ni el del TCP. Si más adelante se quiere Telegram,
se añade como setting opcional sin cambiar el flujo.

---

## 10. Criterios de aceptación

| Caso | Resultado esperado |
|---|---|
| Cliente da todos los datos y confirma | Venta creada con `supplier_id`, `source='bot'`, `order_code`, ledger y reserva. |
| Modo asistido | La venta queda `PENDING_APPROVAL`; no se despacha hasta aprobar. |
| Modo autónomo | La venta pasa a `PENDING_DISPATCH` y se despacha; el cliente recibe “registrado”, no “entregado”. |
| TCP responde `RECIBIDO` | `DISPATCHED → CONFIRMED`; cliente recibe confirmación. |
| TCP responde `ENTREGADO` | `CONFIRMED → DELIVERED`; stock físico baja y reserva se libera. |
| TCP responde `AGOTADO` | `NEEDS_OPERATOR`; no se miente al cliente. |
| Cancelación | Reserva liberada, ledger `VOID`, evento auditado. |
| Comisión en otra moneda | `supplier_payout` NULL; dashboard muestra “requiere conversión”. |
| Stock insuficiente | No se crea venta; se ofrece alternativa. |
| Zona no cubierta | No se crea venta; se ofrecen alternativas o se escala. |
| Reinicio tras crear venta | El despacho se reintenta una vez; no se duplica. |
| Liquidación | Ledger `COLLECTED`, venta `SETTLED`; no se toca stock. |
