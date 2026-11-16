# Guía del TCP (Rolando) — Test con cliente único

## Cuenta
| Rol | Cuenta | Peer ID |
|-----|--------|---------|
| TCP / Proveedor | `@rolandopl` (Rolando Pérez) | `8905117076` |

## Tu misión
El agente de Cuadra te enviará órdenes de venta por mensaje privado. Debes responder con una de estas palabras:

| Respuesta | Significado |
|-----------|-------------|
| `RECIBIDO` | Confirman que preparas la orden |
| `RECIVIDO V-00000X` | Lo mismo, con código de orden (si hay duda) |
| `AGOTADO` | No tienes disponibilidad del producto |
| `ENTREGADO` | Ya entregaste y cobraste al cliente |

## Qué evaluar
- ¿Recibes las órdenes del agente?
- ¿Tu respuesta `RECIBIDO` se aplica a la orden correcta?
- ¿El cliente recibe la confirmación de su pedido?
- ¿Hay mensajes sueltos sin respuesta?

## Feedback rápido (rellena después de cada orden)
| Orden (V-######) | Tú respondiste | Qué esperaba el bot | ¿El cliente recibió confirmación? | ¿OK? |
|------------------|----------------|---------------------|-----------------------------------|------|
| | | | | |
| | | | | |

> Nota: si recibes un mensaje que no es una orden (ej.: "j", "/start"), ignóralo o responde cortésmente que no tienes casos abiertos.
