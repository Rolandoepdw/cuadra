# Análisis de prueba #8 — «accesorios para laptop» no es un detalle de la laptop

**Fecha:** 2026-11-18
**Origen:** prueba real con el operador (conversación 1, Telegram, mensajes 22–23).
**Alcance:** ruta de catálogo para `consulta_dato`, herencia de producto y
higiene de contexto tras una venta entregada. No toca precios, stock, zonas ni
la máquina de ventas.

---

## 1. Problema observado

| # | Turno | Respuesta del bot |
|---|---|---|
| 22 | «Que productos tienes de accesorios para laptop» | «Cuadra no tiene accesorios específicos para laptop en el catálogo. El producto en juego es la Laptop Ultraligera…». |

Hay dos errores en una sola respuesta:

1. **La afirmación es falsa.** El catálogo tiene la categoría `accesorios` con
   tres productos activos: Mouse Inalámbrico (15 USD), Teclado Mecánico
   (45 USD) y Audífonos Bluetooth (35 USD).
2. **Arrastra una venta cerrada.** La Laptop Ultraligera ya se entregó
   (V-000001, stage `DONE`); el cliente abrió una consulta nueva de catálogo y
   el bot respondió como si preguntara un detalle de ese producto.

---

## 2. Diagnóstico

Los trazos del turno muestran que el NLU no falló:

- `llm.classify_light` → `intencion: consulta_dato`, `producto: null`,
  `confianza: 0.95`.
- `pipeline._resolve_product` → `product_id: 2`, `product_source: context`
  (heredó la Laptop Ultraligera). El guard H-09 solo descarta la herencia con
  confianza < 0.35, así que no aplicó.
- `_category_request` **sí resolvía el texto**:
  `("list", ("accesorios", [Mouse, Audífonos, Teclado]))`.

El fallo estaba en el enrutamiento: `_decide` solo consultaba
`_category_request` para `consulta_precio | consulta_stock | comprar`. Con
`consulta_dato` cayó en la rama de detalle, que usó el producto heredado y llamó
al compositor con el prompt «responde con la descripción y especificaciones del
PRODUCTO EN JUEGO». Sin datos de accesorios, el modelo afirmó algo falso.

`_post_sale_outcome` no era el culpable: ya dejaba pasar las peticiones de
catálogo (`_LIST_REQUEST`).

---

## 3. Corrección

En `backend/cuadra/ai/pipeline.py`:

| Cambio | Efecto |
|---|---|
| `_catalog_request(text)`: `_LIST_REQUEST` sí, pero no una pregunta de detalle abierta ni un tema técnico concreto (`_OPEN_DETAILS`, `_STOCK_QUESTION`, `_asked_detail_topics`). | «Qué productos tienes de accesorios» es catálogo; «¿qué procesador trae la laptop?» sigue siendo detalle. |
| `_run_turn` calcula la petición de categoría también para `consulta_dato` y **no hereda** el producto del contexto en un listado (salvo dentro de una venta en curso). | La consulta nueva no responde sobre la laptop entregada. |
| `_decide(..., category_request=…)`: en `consulta_dato`, una petición de catálogo responde con la tabla de la categoría (o con la ficha si el calificador deja un solo producto). Nunca con el compositor. | La tabla sale del catálogo real; no hay afirmaciones negativas inventadas. |
| Tras una venta `DONE`, un listado vuelve a `DISCOVERY` y `_update_context(clear_product=True)` suelta `product_id`, conservando `sale_id`/`order_code`. | El post-venta («¿dónde va mi pedido?») sigue disponible, pero el producto entregado deja de estar «en juego». |

---

## 4. Verificación real

`POST /api/debug/message`, identificador `prueba-8b-2026`, conversación 4:

| Turno | Respuesta |
|---|---|
| «precio de la Laptop Ultraligera» | Ficha: 720 USD, ID 2, 1 unidad, Ryzen 7 · 16GB RAM · 1TB SSD, «¿Te lo reservo?». |
| *contexto forzado a `DONE` / V-000001 / producto 2* | — |
| «Que productos tienes de accesorios para laptop» | Tabla de `accesorios`: Mouse Inalámbrico, Audífonos Bluetooth, Teclado Mecánico. Sin mención a la laptop. |
| «precio de la Laptop Ultraligera» + «¿qué procesador trae la laptop?» | «Esto es lo que tengo de Laptop Ultraligera: Ryzen 7.» |

Estado tras el listado: `stage=DISCOVERY`, `product_id=None`, `sale_id=1`,
`order_code=V-000001`. El turno se resolvió con `generator=template` y
`compose_ms=0`.

---

## 5. Tests

**478 OK** (4 nuevos en `PruebaOchoTests`):

- `test_only_explicit_catalog_requests_take_the_listing_path`
- `test_a_catalog_question_after_a_done_sale_lists_the_accessories`
- `test_a_catalog_question_does_not_inherit_the_previous_product`
- `test_a_detail_question_still_answers_the_product`

`check_all.ps1 -SkipBuild` en verde.

---

## 6. Pendiente (otra rama)

El fix es puntual. La arquitectura acordada queda para después:

1. `catalog_query` única para todas las intenciones.
2. Categorías múltiples por producto y ranking multi-categoría (5–10).
3. Tools de lectura (`search_catalog`, `get_product`, `check_zone`) con el
   golden set como medida.
4. Grounding de afirmaciones negativas («no hay X»).
