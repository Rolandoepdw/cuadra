# Guía del Cliente — Test con Rolando (TCP único)

## Cuentas involucradas
| Rol | Cuenta | Propósito |
|-----|--------|-----------|
| TCP / Proveedor | `@rolandopl` (Rolando Pérez) | Recibe órdenes y responde RECIBIDO/AGOTADO/ENTREGADO |
| Cliente | Tu cuenta personal | Escribe al agente (userbot `@repdcuadra`) |
| Agente / Bot | `@repdcuadra` (userbot de Cuadra) | Responde precios, stock, zonas, crea pedidos |

## Productos disponibles (solo Rolando)
Puedes preguntar al agente por cualquiera de estos. No necesitas entrar a ningún grupo: esta tabla es lo que verías en el canal de difusión.

| #   | Producto                 | Precio   | Stock | Zona de entrega |
| --- | ------------------------ | -------- | ----- | --------------- |
| 1   | Laptop Empresarial       | 350 USD  | 4     | Playa           |
| 2   | Laptop Ultraligera       | 720 USD  | 2     | Playa           |
| 3   | Monitor LED 24           | 180 USD  | 6     | Diez de Octubre |
| 4   | Tablet Android 10        | 220 USD  | 5     | Marianao        |
| 5   | Aspiradora Portátil      | 95 USD   | 8     | Centro Habana   |
| 6   | Freidora de Aire         | 130 USD  | 5     | La Lisa         |
| 17  | PC de Escritorio Ryzen 5 | 480 USD  | 3     | Diez de Octubre |
| 18  | Impresora Multifuncional | 210 USD  | 4     | Marianao        |
| 19  | Mouse Inalámbrico        | 15 USD   | 25    | Playa           |
| 20  | Teclado Mecánico         | 45 USD   | 15    | Diez de Octubre |
| 21  | Audífonos Bluetooth      | 35 USD   | 20    | La Lisa         |
| 32  | Combo Personalizado      | 60 USD   | 10    | Marianao        |
| 39  | Laptop Gamer RTX 4060    | 1100 USD | 1     | Playa           |

## Guion breve (4–5 pasos)
1. Saluda y pregunta por un producto (ej.: "¿Cuánto cuesta la Tablet Android 10?").
2. Pide precio, stock y zona de entrega si el bot no los da.
3. Dime que quieres comprarlo ("me la llevo", "resérvame", "quiero 1").
4. Dale tus datos: zona, dirección (calle, número), teléfono.
5. Confirma el pedido cuando el bot te pregunte.

## Feedback rápido (rellena después de cada paso)
Copia esta tablita y rellena con lo que ocurrió. No necesitas capturas ni horarios exactos.

| Paso | Tú escribiste | Qué quisiste decir | Qué esperabas | Qué respondió el bot | ¿OK? |
|------|---------------|--------------------|---------------|----------------------|------|
| 1 | | | | | |
| 2 | | | | | |
| 3 | | | | | |
| 4 | | | | | |
| 5 | | | | | |

## Qué evaluar
- ¿El bot responde precio exacto con moneda?
- ¿Responde zona de entrega correcta (la del producto, no inventada)?
- ¿Crea la venta cuando confirmas?
- ¿Los mensajes llegan sin demoras excesivas?
- ¿Hay placeholders como `[precio anterior]` o `[DIR]`?
