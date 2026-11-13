# Guía del proveedor (TCP) — test de Cuadra

Haces de almacén de una tienda. El bot te escribe por Telegram cuando un cliente
compra o pregunta, y tú respondes **por ese mismo chat**.

## 1. Tu listado

Busca tu nombre y usa solo esa tabla. Las zonas son donde entregas.

### Claudia — zonas: Playa, Marianao, Centro Habana

| Producto | Precio | Moneda | Stock | Categoría |
|---|---|---|---|---|
| Laptop Gamer RTX 3050 | 850 | USD | 2 | tecnología |
| Cafetera Express | 160 | USD | 6 | hogar |
| Power Bank 20000 | 40 | USD | 0 (agotado) | accesorios |
| Cable HDMI 2 m | 8 | USD | 40 | accesorios |
| Microondas 20L | 140 | USD | 3 | hogar |
| Ventilador de Torre | 55 | USD | 6 | hogar |
| Cargador Rápido 20W | 18 | USD | 30 | accesorios |

### Roger — zonas: La Lisa, Marianao

| Producto | Precio | Moneda | Stock | Categoría |
|---|---|---|---|---|
| Moto Eléctrica Urban | 1350 | USD | 4 | motos_eléctricas |
| Moto Eléctrica Cargo | 2500 | USD | 2 | motos_eléctricas |
| Moto Eléctrica Sport | 1800 | USD | 3 | motos_eléctricas |
| Moto Eléctrica Urban Plus | 1500 | USD | 2 | motos_eléctricas |
| Moto Combustión Yamaha 150 | 2100 | USD | 3 | motos_combustión |
| Moto Combustión Haojue 200 | 2300 | USD | 2 | motos_combustión |
| Pan con Croqueta | 300 | CUP | 150 | alimentos |
| Café con Leche | 150 | CUP | 300 | alimentos |
| Combo Desayuno | 450 | CUP | 80 | alimentos |
| Pizza Personal | 350 | CUP | 60 | alimentos |
| Jugo Natural | 200 | CUP | 120 | alimentos |
| Papas Fritas | 180 | CUP | 200 | snacks |
| Refresco Cola | 220 | CUP | 180 | snacks |
| Chocolate con Leche | 260 | CUP | 120 | snacks |
| Maní Salado | 150 | CUP | 150 | snacks |

### Rolando — zonas: Playa, La Lisa, Boyeros, San Miguel, Arroyo Naranjo, Cerro, Cotorro, Diez de Octubre, Guanabacoa, Marianao, Plaza de la Revolución, Regla

| Producto | Precio | Moneda | Stock | Categoría |
|---|---|---|---|---|
| Laptop Empresarial | 350 | USD | 4 | tecnología |
| Laptop Ultraligera | 720 | USD | 2 | tecnología |
| Laptop Gamer RTX 4060 | 1100 | USD | 1 | tecnología |
| Router WiFi 6 | 90 | USD | 4 (pausado) | tecnología |
| Monitor LED 24 | 180 | USD | 6 | tecnología |
| Tablet Android 10 | 220 | USD | 5 | tecnología |
| PC de Escritorio Ryzen 5 | 480 | USD | 3 | computación |
| Impresora Multifuncional | 210 | USD | 4 | computación |
| Aspiradora Portátil | 95 | USD | 8 | hogar |
| Freidora de Aire | 130 | USD | 5 | hogar |
| Mouse Inalámbrico | 15 | USD | 25 | accesorios |
| Teclado Mecánico | 45 | USD | 15 | accesorios |
| Audífonos Bluetooth | 35 | USD | 20 | accesorios |
| Combo Personalizado | 60 | USD | 10 | sin categoría |

### Yoan — zonas: Habana del Este, Guanabacoa, Regla

| Producto | Precio | Moneda | Stock | Categoría |
|---|---|---|---|---|
| Moto Eléctrica Delivery | 1600 | USD | 3 | motos_eléctricas |
| Moto Eléctrica City | 1200 | USD | 3 | motos_eléctricas |
| Moto Combustión Zontes 250 | 2600 | USD | 2 | motos_combustión |
| Batería para Moto Eléctrica | 250 | USD | 5 | accesorios |
| Casco para Moto | 40 | USD | 10 | accesorios |

## 2. Qué entiende el bot según lo que escribas

| Escribes | El bot entiende |
|---|---|
| `RECIBIDO` | Confirmas y preparas el pedido; la venta pasa a confirmada y avisan al cliente. |
| `AGOTADO` o `SIN STOCK` | No hay disponibilidad; la venta queda pendiente del operador y avisan al cliente. |
| `ENTREGADO` o `COBRADO` | Ya entregaste y cobraste; la venta se cierra como entregada. |
| `[V-000042] RECIBIDO` | Confirmas **esa** orden concreta. |
| `[E-0104] sí, es compatible` | Contestas **esa** consulta concreta. |
| Una respuesta que nombra el producto, sin código | En una **consulta**, el bot la busca por el producto. En una **orden**, el nombre no cuenta: usa la orden abierta más antigua. |
| Una respuesta sin código ni producto | Si solo hay una abierta, la usa; si hay varias, elige una (normalmente la más antigua). |
| `ahora cuesta 18 USD` o `quedan 3` | Propuesta de cambio de catálogo; el operador la aprueba o la rechaza. |

## 3. Casos a realizar

Hazlos en este orden. **T3 y T8** necesitan dos casos abiertos a la vez: el
organizador te avisa cuándo hacerlos.

| # | Caso | Qué escribir | Resultado (lo completas tú) |
|---|---|---|---|
| T1 | Confirmar simple | `RECIBIDO` | ¿La venta quedó confirmada? ¿Le avisaron al cliente? |
| T2 | Confirmar con referencia | `[V-…] RECIBIDO` (la referencia va escrita en el texto) | ¿Reconoció esa orden concreta? |
| T3 | Dos órdenes abiertas | `RECIBIDO` sin referencia | ¿Cuál confirmó? ¿Era la que querías? |
| T4 | Rechazar | `AGOTADO, no me queda ninguno` | ¿Quedó pendiente del operador sin cancelarse? |
| T5 | Entregar | `ENTREGADO` | ¿Se cerró la venta como entregada? |
| T6 | Consulta con código | `[E-…] sí, es compatible` | ¿Le llegó la respuesta al cliente que preguntó? |
| T7 | Consulta sin código | `el <producto> sí es compatible` | ¿Acertó la consulta por el nombre del producto? |
| T8 | Dos consultas abiertas | `sí, hay` sin código | ¿Acertó a cuál respondía o eligió la equivocada? |
| T9 | Precio | `el <producto> ahora cuesta 18 USD` | ¿Propuso el cambio de precio al operador? |
| T10 | Stock | `me quedan 3 <productos>` | ¿Propuso el cambio de stock al operador? |

## 4. Registro de los mensajes

Apunta **una fila por respuesta tuya**, mientras respondes. La primera fila es un
ejemplo.

| # | Hora | Recibí (V-… / E-…) | Respondí | A qué me refería | ¿Era ambiguo? | ¿Acertó el bot? |
|---|---|---|---|---|---|---|
| 1 | 14:20 | Orden V-000042 | RECIBIDO | esa orden | no | sí |
| 2 | | | | | | |
| 3 | | | | | | |
| 4 | | | | | | |
| 5 | | | | | | |
| 6 | | | | | | |
| 7 | | | | | | |
| 8 | | | | | | |
| 9 | | | | | | |
| 10 | | | | | | |
| 11 | | | | | | |
| 12 | | | | | | |

## 5. Cierre

| Pregunta | Respuesta |
|---|---|
| ¿Qué mensaje te costó más entender o responder? | |
| ¿El bot acertó cuando tu respuesta era ambigua? | |
| ¿Algo te resultó confuso o incómodo? | |

## 6. Entrega

Guarda el archivo como `T1_registro.md` (con tu código) y mándaselo al
organizador por el canal que te indique.
