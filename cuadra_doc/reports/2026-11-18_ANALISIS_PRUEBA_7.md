# Análisis de prueba #7 — el «sí» tras la ficha inicia la venta

**Fecha:** 2026-11-18
**Origen:** prueba real con el operador (conversación 1, Telegram).
**Alcance:** cierre de la ficha y consumo de la oferta `RESERVE`; no toca
catálogo, zonas ni ventas ya cerradas.

---

## 1. Problema observado

| # | Turno | Respuesta del bot |
|---|---|---|
| 1 | «Hola» | Saludo. |
| 2 | «Que productos tienes disponibles» | «Dime cuál producto te interesa…». |
| 3 | «me interesa una laptop» | Tabla de laptops. |
| 4 | Audio «Quiero la otra ligera.» | Ficha de Laptop Ultraligera con «¿Te lo reservo?». |
| 5 | **«si»** | **Volvió a enviar la tabla de laptops.** |

El cliente aceptó la reserva y el bot lo trató como una nueva consulta de
catálogo.

---

## 2. Diagnóstico

Los trazos del turno 5 muestran que el NLU no falló:

- `llm.classify_light` → `intencion: comprar`, `producto: "Laptop Ultraligera"`,
  `confianza: 0.95`.
- `pipeline._resolve_product` → `product_id: 2` (Laptop Ultraligera).
- La oferta `RESERVE` de la ficha se consumió correctamente.

El fallo estaba en `_category_request`:

1. La palabra «si» no está en `_CATEGORY_FILLER`, así que contaba como
   contenido.
2. El término «laptop» se tomaba del **eco del NLU** (`intent.producto`), no del
   texto del cliente.
3. «si» quedaba como «calificador»; `narrow_products` lo ignora por tener menos
   de 4 letras y devuelve la familia completa, así que `_category_request`
   respondía `("list", …)`.
4. `_maybe_handle_sale` interpreta un listado como consulta de catálogo y
   devuelve `None`; `_decide` renderizaba la tabla y la venta nunca avanzaba.

---

## 3. Corrección

En `backend/cuadra/ai/pipeline.py`:

| Cambio | Efecto |
|---|---|
| `_category_request` devuelve `None` si `offers.answer(text)` no es `None`. | Un «sí»/«no» desnudo responde a la pregunta anterior (la oferta); nunca es una petición de familia. |
| Confirmaciones añadidas a `_CATEGORY_FILLER` (`si`, `sí`, `dale`, `ok`, `okay`, `perfecto`, `confirmo`, `confirmado`, `listo`, `seguro`, `obvio`). | No cuentan como contenido que califique la categoría. |
| `rest` solo conserva palabras de ≥ 4 letras, la misma regla que aplica `narrow_products`. | Una palabra corta no simula un calificador. |

La oferta sigue siendo la única fuente de verdad para un «sí» suelto: se
consume en el turno y no puede responderse dos veces.

---

## 4. Verificación real

Core reiniciado en `http://127.0.0.1:8321`; `POST /api/debug/message` con el
identificador `prueba-si-2026` (sin clientes reales):

| Turno | Resultado |
|---|---|
| «Hola» | Saludo, sin zona ni producto. |
| «ando buscando una laptop» | Tabla con las 3 laptops del catálogo. |
| «precio de la Laptop Ultraligera» | Ficha: 720 USD, ID 2, 2 unidades, Ryzen 7/16GB/1TB, «¿Te lo reservo?». |
| **«si»** | «Son 720 USD por 1 x Laptop Ultraligera. Todavía me falta tu nombre, la zona de entrega, la dirección (calle, número y entrecalles), un teléfono de contacto…». |

No se repitió el listado y no se creó la venta: el «sí» inicia la captura de
datos, que es el paso correcto antes de confirmar.

---

## 5. Pruebas

- Nuevo test de regresión:
  `PruebaCincoTests.test_a_bare_yes_after_the_card_starts_the_sale`.
  Ficha → «si» → exige que no haya tabla, que no aparezca «Dime el ID o el
  nombre» y que pida los datos de la venta.
- Suite completa: **474 tests OK**.
- `check_all.ps1 -SkipBuild`: **All green**.
