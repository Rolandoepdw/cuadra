# Análisis de prueba #6 — zona en su turno y ficha completa

**Fecha:** 2026-11-18
**Origen:** prueba real con el operador.
**Alcance:** conversación de catálogo y entrega; no toca ventas ya cerradas.

---

## 1. Problemas observados

| # | Síntoma | Causa |
|---|---|---|
| F1 | El saludo y la cotización mencionaban la zona de entrega antes de que el cliente la pidiera. | `greeting_product` incluía `{zona}`; `_reply_zone` elegía la zona habitual o listaba hasta 3 municipios. |
| F2 | Al pedir «una laptop» el bot ofrecía un solo producto. | El listado solo se activaba con una categoría «desnuda»; «ando buscando» no era relleno y `find_product` hacía fuzzy match con un producto concreto. |
| F3 | La zona del proveedor se trataba como la zona de todos sus productos. | No existía cobertura por producto; `sales` solo miraba `supplier_delivery_zones`. |

---

## 2. Cambios

- **La zona espera su turno.** `_render_product_card` reemplaza a
  `greeting_product`. La ficha lleva nombre, precio, ID, disponibilidad,
  especificaciones, transporte y cierre; **no** lleva zona. Solo se responde
  cobertura si el cliente la pregunta o al capturar la dirección. Nunca se asume
  la `zona_habitual`.
- **Zonas por producto.** Migración `023_product_delivery_zones.sql`. Sin filas =
  hereda el TCP; con filas = intersección con la cobertura del TCP. La venta, las
  respuestas de cobertura y `{zona}` de las difusiones usan
  `catalog.product_covers_zone`.
- **Familia, no producto al azar.** `_category_request` reconoce peticiones
  conversacionales y lista hasta 10 productos (incluidos `OUT_OF_STOCK`,
  marcados «agotado»). Si un calificador deja uno, o la familia solo tiene uno,
  se responde con la ficha.
- **Zonas cortas en preguntas de entrega.** `_ZONE_HINT_RE` reconoce
  «entregan/entregamos/entrego/envían», así que «¿Entregan en Playa?» resuelve la
  zona aunque tenga menos de 6 letras.

---

## 3. Verificación real

Core en `http://127.0.0.1:8321`, migración 023 aplicada, `POST
/api/debug/message` con el identificador `prueba-zonas-2026` (sin clientes
reales):

| Turno | Resultado |
|---|---|
| «Hola» | Saludo, sin zona ni producto. |
| «ando buscando una laptop» | Tabla con las 3 laptops (IDs 1, 2, 13), incluida la de última unidad. |
| «precio de la Laptop Ultraligera» | Ficha: 720 USD, ID 2, 2 unidades, Ryzen 7/16GB/1TB, transporte coordinado, «¿Te lo reservo?». Sin zona. |
| «¿Entregan en Playa?» (producto restringido a Marianao) | «Esa zona no la cubre el almacén de Laptop Ultraligera…». |
| «¿Entregan en Marianao?» | «Sí, en Marianao sí entregamos Laptop Ultraligera…». |
| «¿Puedes entregar en mi zona?» | Solo la cobertura efectiva del producto (Marianao). |

La restricción de prueba se revirtió (`delivery_zone_ids = []`) y la conversación
de depuración quedó cerrada.

---

## 4. Fallos encontrados al verificar

1. `catalog.product_covers_zone` caía a la cobertura del TCP cuando el producto
   tenía zonas propias pero no incluía la consultada. Ahora exige la zona en la
   lista del producto.
2. `resolve_zones` no reconocía «entregan» como pista; una zona corta como
   «Playa» no resolvía en «¿Entregan en Playa?».

---

## 5. Pruebas

- Suite completa: **472 tests OK** (10 nuevos).
- Nuevos: petición conversacional de categoría, agotados en el listado, ficha
  completa, familia de un solo producto, herencia y restricción de zonas, venta
  rechazada por zona del producto, zona que el TCP no cubre y pregunta de
  cobertura por producto.
- `npm run build` del dashboard en verde.

---

## 6. Pendientes

- Confirmar en Telegram real el render de la tabla y de la ficha (Markdown del bot
  y del userbot).
- Revisar visualmente el selector de zonas por producto en el dashboard.
- Evaluar paginación del listado si una categoría supera 10 productos.
