---
tipo: cierre_fase
proyecto: Cuadra
modulo: ventas_y_finanzas
estado: FASE 3 — implementada, pendiente operación real
fecha: 2026-11-06
tags: [fase-3, ventas, stock-reservado, ledger, comisiones, tcp, finanzas]
---

# 09 — Cierre de la Fase 3

> Venta de punta a punta, stock reservado, libro de comisiones, despacho al TCP
> por Telegram y panel de finanzas. **Sin tocar la base real ni la operación**:
> todo se probó con bases SQLite temporales y el envío real al TCP queda detrás
> de la cola de trabajos.

---

## 1. Qué cambió

| Archivo | Cambio |
|---|---|
| `migrations/014_sales_and_finance.sql` | `products.stock_reserved` y `commission_type`; rebuild de `sales` con la máquina de estados final; `commission_ledger` con backfill; settings de autonomía. |
| `domain/sales.py` (nuevo) | Reserva atómica, cálculo de comisión, transiciones, acks del TCP, ledger y resumen por moneda. |
| `ai/memory.py` | Hecho `delivery_address`, extracción determinista de direcciones y normalización de teléfono delegada a `sales.normalize_phone`. |
| `ai/prompts.py` | Plantillas de captura, confirmación, aviso al cliente y orden al TCP (sin comisión ni margen). |
| `ai/pipeline.py` | Slots de venta (`CAPTURE_DATA` → `CONFIRM` → `DONE`/`HANDOFF`), creación de la venta y bloqueo de ventas duplicadas por conversación. |
| `ai/handlers.py` | Job `dispatch_sale`, recuperación de despachos pendientes al arrancar, acks de venta en `tcp_reply` y aviso al cliente. |
| `channels/telegram.py`, `channels/telegram_user.py` | `send_text` devuelve el id del mensaje del proveedor para correlacionar el despacho. |
| `channels/base.py` | Contrato de `send_text` actualizado a `str | None`. |
| `api/routes_finance.py` (nuevo) | `/api/finance/*`: resumen, ventas, ledger, aprobar, despachar, liquidar y cancelar. |
| `api/models.py`, `domain/catalog.py` | `commission_type` (`FIXED`/`PERCENT`) editable desde el catálogo. |
| `main.py` | Registra el router de finanzas y rearma despachos pendientes en el arranque. |
| `domain/events.py` | Eventos `sale_*`, `tcp_sale_ack` y `commission_settled` en la ventana de conversaciones. |
| `dashboard/src/panels/finance/finance_panel.tsx` (nuevo) | Tarjetas, totales por moneda, ventas, ledger y acciones del operador. |
| `dashboard/src/types/api.ts`, `lib/api.ts` | Contratos y cliente de finanzas; `commission_type` en el catálogo. |
| `dashboard/src/App.tsx`, `components/layout/sidebar.tsx` | Ruta y entrada de menú **Finanzas**. |
| `dashboard/src/panels/catalog/products_tab.tsx` | Selector de tipo de comisión (monto fijo o porcentaje). |

Settings nuevos (en `settings`, no en `.env`):

- `sales.mode` = `assisted` (default), `hybrid` o `autonomous`.
- `sales.hybrid_max_age_hours` = 24.
- `catalog.autoapply.*`, `catalog.price_max_delta_pct`, `catalog.autoapply.min_confidence`
  quedan preparados para la Fase 4; esta fase no los lee.

---

## 2. Correcciones al diseño aprobado

- **`sales.commission_currency` no existía** en `001_init.sql`. La migración `014`
  la deriva de `products.commission_currency` para las filas históricas y la guarda
  explícitamente en las ventas nuevas. Así el histórico conserva su moneda real.
- **`sales.tcp_message_id`** se añadió al rebuild aunque no estaba en el DDL del
  documento: sin ese id no se puede correlacionar el mensaje de despacho con la
  respuesta del TCP cuando hay varias órdenes abiertas.
- **Rebuild de `sales`**: SQLite no permite cambiar un `CHECK` con `ALTER TABLE`.
  Se usó el mismo patrón de `006` y `011` (`foreign_keys=OFF`, tabla nueva, copia,
  `DROP`, `RENAME`, índices). `PAID` se traduce a `SETTLED`.
- **Backfill del ledger**: las ventas no canceladas anteriores a la fase entran al
  libro; las que estaban `PAID`/`SETTLED` quedan `COLLECTED`.
- **Resumen sin `domain/finance.py`**: las métricas viven en `sales.summary()` para
  no duplicar consultas ni abrir una capa vacía. El panel consume el mismo contrato
  descrito en `02` §7.1.
- **`sale_followup` sin job**: el documento `02` no define el contenido del job y
  D6-A prohíbe notificar al operador por Telegram. El recordatorio vive solo en
  el dashboard (`GET /api/finance/followups` y la card **Seguimiento**).

---

## 3. Máquina de estados

```text
DRAFT
  └─(asistido/híbrido con reserva)─> PENDING_APPROVAL
  └─(autónomo / híbrido confiable)─> PENDING_DISPATCH
PENDING_APPROVAL ─(operador aprueba)─> PENDING_DISPATCH
PENDING_DISPATCH ─(job dispatch_sale)─> DISPATCHED
DISPATCHED ─(TCP: RECIBIDO)─> CONFIRMED
DISPATCHED | CONFIRMED ─(TCP: ENTREGADO)─> DELIVERED
DELIVERED ─(operador cobra)─> SETTLED
antes de DELIVERED ─(operador cancela)─> CANCELLED
TCP: AGOTADO / error de despacho ─> NEEDS_OPERATOR
NEEDS_OPERATOR ─(operador rearma)─> PENDING_DISPATCH
```

Reglas implementadas:

- Una venta = un producto. `supplier_id` sale del producto; `source` solo `bot` u
  `operator`; `order_code = V-{id:06d}` se escribe después del insert.
- La reserva usa `stock_reserved`, nunca descuenta `stock` físico hasta `DELIVERED`.
- La reserva y el ledger se escriben en la misma transacción: si el stock no alcanza,
  no queda venta ni ledger.
- `cancel` libera la reserva con `MAX(0, ...)` y deja el ledger en `VOID`.
- `deliver` mueve stock físico y reserva; si el stock físico no alcanza, la venta
  pasa a `NEEDS_OPERATOR` y no se miente al cliente.
- Al reiniciar, `recover_pending_dispatches()` reencola solo ventas
  `PENDING_DISPATCH` que no tengan ya un job vivo; el job es idempotente por estado.

---

## 4. Comisiones y moneda

```text
FIXED    -> commission_earned = commission * quantity
            moneda = products.commission_currency
PERCENT  -> commission_earned = total * commission / 100
            moneda = moneda de la venta
payout   -> total - commission_earned  (solo si las monedas coinciden)
```

- Si la moneda de la comisión no coincide con la de la venta, `supplier_payout`
  queda `NULL` y el panel muestra **“requiere conversión”**.
- `summary()` agrupa por moneda de venta y solo suma comisiones de esa misma moneda.
  Las comisiones cruzadas se cuentan aparte en `conversion_pending`; nunca se suman
  USD y CUP en un total.
- Liquidar mueve el ledger `PENDING → COLLECTED` y la venta `DELIVERED → SETTLED`;
  no toca stock.

---

## 5. Bucle TCP

1. La venta llega a `PENDING_DISPATCH` (aprobación del operador o modo autónomo).
2. El job `dispatch_sale` envía al `telegram_id` del TCP la orden con
   `order_code`, producto, cantidad, total a cobrar en destino y datos de entrega.
3. `send_text` devuelve el id del mensaje y se guarda en `sales.tcp_message_id`.
4. La respuesta del TCP entra por el mismo canal de proveedores:
   - `V-######` correlaciona exacto; sin código se toma la venta despachada más
     antigua del proveedor.
   - `RECIBIDO` → `CONFIRMED`.
   - `ENTREGADO` / `COBRADO` → `DELIVERED` (o `NEEDS_OPERATOR` si falta stock).
   - `AGOTADO` / `SIN STOCK` → `NEEDS_OPERATOR`.
   - Cualquier otro texto sigue el flujo de escalaciones de la Fase 2.
5. El cliente recibe la plantilla correspondiente (`sale_confirmed`,
   `sale_delivered` o `sale_unavailable`). No se envía nada al Telegram del operador
   (D6-A): las alertas viven en el panel de Finanzas.

---

## 6. Panel de Finanzas

- Tarjetas: ventas hoy, por aprobar, despachadas y entregadas por liquidar.
- Avisos de `NEEDS_OPERATOR` y de comisiones que requieren conversión.
- Totales por moneda: venta bruta, comisión generada, cobrada y pendiente.
- Tabla de ventas con orden, producto, cliente, TCP/zona, total, comisión, estado,
  fecha y acciones `[Aprobar]`, `[Despachar]`, `[Cobrar comisión]`, `[Cancelar]`.
- Tabla del libro de comisiones con TCP, pago al TCP y estado del cobro.
- Filtros por estado y moneda.
- Ruta `/finance` y entrada **Finanzas** en el menú lateral.

Las acciones llaman a `approve`, `dispatch`, `settle` y `cancel` de la API; las dos
primeras encolan `dispatch_sale`. La UI está en español; el código y los comentarios,
en inglés.

---

## 7. Aceptación

| Criterio (doc 02 §10) | Estado |
|---|---|
| Cliente da los datos y confirma → venta, ledger y reserva | ✅ `test_sale_reserves_stock_and_writes_the_ledger`. |
| Modo asistido queda en `PENDING_APPROVAL` | ✅ `test_assisted_flow_runs_until_the_commission_is_settled`. |
| Modo autónomo pasa a `PENDING_DISPATCH` | ✅ `test_autonomous_mode_starts_in_pending_dispatch`. |
| TCP `RECIBIDO` → `CONFIRMED` | ✅ `test_received_then_delivered_edit_the_same_sale_by_order_code`. |
| TCP `ENTREGADO` → `DELIVERED`, stock físico baja | ✅ mismo caso + `test_assisted_flow_...`. |
| TCP `AGOTADO` → `NEEDS_OPERATOR` | ✅ `test_out_of_stock_flags_the_operator`. |
| Cancelación → reserva liberada, ledger `VOID` | ✅ `test_cancel_releases_the_reservation_and_voids_the_ledger`. |
| Comisión en otra moneda → `supplier_payout` NULL | ✅ `test_fixed_commission_in_another_currency_never_mixes_money`. |
| Stock insuficiente → no se crea venta | ✅ `test_sale_is_all_or_nothing_when_stock_is_exhausted` y `test_quantity_over_available_...`. |
| Zona no cubierta → no se crea venta | ✅ `test_supplier_must_cover_the_delivery_zone`. |
| Reinicio tras crear venta → despacho una vez, sin duplicar | ✅ job idempotente por estado + `recover_pending_dispatches()`; cubierto por el diseño y las pruebas de transición. Queda una ventana mínima si el proceso muere entre el envío aceptado por Telegram y el guardado de `DISPATCHED` (ver limitaciones). |
| Liquidación → ledger `COLLECTED`, venta `SETTLED` | ✅ `test_settling_moves_the_commission_from_pending_to_collected`. |

Pruebas: **97 casos** en `unittest` (24 de Fase 0 + 19 de Fase 1 + 18 de Fase 2 +
**36 de Fase 3**), todos en verde. El dashboard compila con `tsc -b && vite build`.

```powershell
cd backend
.venv\Scripts\python.exe -m unittest discover -s tests -q
cd ..
powershell -ExecutionPolicy Bypass -File .\build_dashboard.ps1
```

La migración `014` se verificó sobre una base temporal nueva: las 14 migraciones
aplican, la tabla `sales` queda con las columnas y estados finales, el ledger y los
settings existen y `PRAGMA quick_check` devuelve `ok`. **La base real no se tocó.**

---

## 8. Limitaciones de esta fase

- **Modo de autonomía**: `sales.mode` (`assisted` por defecto) ya se edita desde
  la card **Modo de venta** de Finanzas; `autonomous` pide confirmación.
- **Notificación al TCP al cancelar**: implementada el 2026-11-11 (bitácora
  §10.16). Se avisa por Telegram solo si la orden ya había llegado al TCP
  (`DISPATCHED`/`CONFIRMED`); antes del despacho no hay nada que cancelar.
- **Aviso al cliente al cancelar**: implementado el 2026-11-11 (bitácora §10.18).
  Siempre que la venta tenga conversación, el cliente recibe `sale_cancelled` por
  el *outbox* de respuestas; el mensaje es idempotente por venta.
- **`sale_followup` solo en el dashboard**: la card **Seguimiento** lista las
  ventas abiertas sin cambios en 12/24/48/72 h (bitácora §10.19). No hay
  recordatorio automático al cliente; queda anotado para la v2, donde habrá que
  definir texto, umbral y política anti-spam.
- **Cancelar después de `DELIVERED`**: no se permite por API; requiere decisión
  manual del operador, tal como pide §5. La venta entregada ya movió stock físico.
- **Conversión de moneda**: no hay tabla de tasas. Las comisiones cruzadas quedan
  pendientes de conversión y se muestran, no se convierten.
- **Multiproducto**: diferido por D5; una venta sigue siendo un producto.
- **Despacho real**: requiere el bot de Telegram con `telegram_id` en el TCP. Sin
  red o sin `telegram_id`, la venta pasa a `NEEDS_OPERATOR` y no se pierde.
- **Ventana de doble despacho**: si el proceso muere después de que Telegram acepta
  el mensaje y antes de guardar `DISPATCHED`, el reinicio puede reenviar la orden.
  Telegram no ofrece clave de idempotencia; el TCP ve el mismo `order_code` y puede
  ignorar el duplicado. Es una ventana mínima, no un despacho continuo.
- **Bake-off, p95 real y recall@3**: siguen pendientes de medición con el modelo y
  las consultas reales; no se inventan cifras.
- **Commit**: la Fase 3 se commiteó el 2026-11-11 junto con las fases 0-5
  (`0c2917c`); el árbol quedó limpio.

---

## 9. Próximo paso

1. Revisar este diff con el dueño; **no se hace commit sin pedido explícito**.
2. Probar una venta real en modo asistido con un TCP de prueba: aprobar, despachar,
   recibir `RECIBIDO`/`ENTREGADO` y liquidar la comisión.
3. Decidir si `sales.mode` necesita selector en el dashboard antes de pasar a la Fase 4.
4. Implementar la Fase 4 (`015_catalog_mutations.sql`, triage TCP, mutaciones y undo).
5. Medir el p95 real cuando el servidor llama.cpp esté levantado.
