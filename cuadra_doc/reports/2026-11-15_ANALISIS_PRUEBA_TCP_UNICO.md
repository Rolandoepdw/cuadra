# Analisis de prueba — Test individual con Proveedor Tres (TCP unico)

**Fecha:** 2026-11-15, 06:29–06:47 UTC
**Canal:** Telegram userbot (`@cuadra_demo_userbot`)
**Configuración:** `sales.mode = autonomous`, `sales.followup.mode = shadow`
**Base de datos:** `backend/data/cuadra.db` (sembrada con `seed_single_supplier.py` + `seed_real_supplier.json`)
**Cuentas:** Cuadra (userbot), Proveedor Tres (TCP/@proveedor_tres), Usuario personal (cliente)

---

## 1. Resumen ejecutivo

Se completó una venta (V-000001, Tablet Android 10, $220 USD, Marianao, SETTLED) y el flujo TCP funcionó (RECIBIDO → ENTREGADO). Sin embargo, la conversación estuvo marcada por **repeticiones de plantillas**, **pérdida de contexto**, **cantidad ignorada** y una **escalación mal formulada**. El cliente tuvo que repetir su dirección 4 veces y el bot no captó que quería "dos tablets".

| Métrica | Valor |
|---|---|
| Conversaciones | 2 (1 cliente, 1 TCP) |
| Mensajes | 40 (16 cliente, 19 bot, 5 TCP) |
| Ventas | 1 (V-000001, SETTLED) |
| Escalaciones | 1 (RESOLVED, 9.5 min) |
| Jobs | Todos COMPLETED, 0 fallidos |
| Llamadas LLM | Todas OK, 0 errores |
| Latencia p50 | ~2,5–4,5 s (dominado por `intent`) |

El sistema **no se cayó**; hizo exactamente lo que el código dice. Los problemas son de lógica determinista, no de infraestructura.

---

## 2. Evidencia por mensaje (conversación c1 = cliente)

| Msg | Hora | Autor | Contenido | Problema |
|-----|------|-------|-----------|----------|
| m1 | 06:29:22 | CLI | "Hola" | — |
| m2 | 06:29:25 | CLI | "buenas noches" | — |
| m3 | 06:29:27 | BOT | "¡Buenas! Dime cuál producto te interesa..." | **Repetición** (ver m4) |
| m4 | 06:29:30 | BOT | "¡Buenas! Dime cuál producto te interesa..." | **Misma plantilla que m3** |
| m5 | 06:29:45 | CLI | "Me interesa la tablet" | — |
| m6 | 06:29:49 | BOT | "¡Buenas! Sí, tenemos Tablet Android 10..." | — |
| m7 | 06:30:59 | CLI | "Vale, cuantas les quedan disponible, además me interesa saber las zonas a las que entregan" | — |
| m8 | 06:31:04 | BOT | "¡Buenas! Sí, tenemos Tablet Android 10..." | **Misma plantilla que m6** |
| m9 | 06:32:01 | CLI | "Tienes domicilio a la UCI" | UCI es zona inactiva |
| m10 | 06:32:05 | BOT | "Ese detalle no lo tengo a mano, pero le consulto al almacén..." | **Escalación innecesaria** (ver m11) |
| m11 | 06:33:08 | TCP | "A q te refieres con ubicación exacta, pregunta nuestra dirección?" | TCP confuso |
| m12 | 06:33:15 | BOT | "Ya me confirmó el almacén: A q te refieres con ubicación exacta..." | **Slot crudo filtrado al cliente** |
| m13 | 06:34:20 | CLI | "Te preguntaba si entregabas la tablet en la UCI..." | Cliente repite |
| m14 | 06:34:23 | BOT | "¡Buenas! Sí, tenemos Tablet Android 10..." | **Repetición, no responde a UCI** |
| m15 | 06:35:27 | TCP | "Si es si hacemos domicilio, fijate en mis datos..." | TCP confuso |
| m16 | 06:36:16 | CLI | "Vale, quiero dos tablet" | Cliente pide cantidad 2 |
| m17 | 06:36:20 | BOT | "Para dejarte el pedido listo me falta la zona de entrega..." | **Olvida que ya pidió precio** |
| m18 | 06:38:15 | CLI | "Calle Prueba 110, entre Calle B y Calle A, número 110" | — |
| m19 | 06:38:20 | BOT | "¡Buenas! Dime cuál producto te interesa..." | **Resetea contexto, ignora dirección** |
| m20 | 06:38:40 | CLI | "Quiero comprar dos tablet" | Reitera cantidad 2 |
| m21 | 06:38:44 | BOT | "Para dejarte el pedido listo me falta la zona de entrega..." | **Pide dirección otra vez** |
| m22 | 06:39:02 | CLI | "Calle Prueba 110..., 55500000" | Dirección + teléfono |
| m23 | 06:39:06 | BOT | "¡Buenas! Dime cuál producto te interesa..." | **Resetea contexto otra vez** |
| m24 | 06:40:00 | CLI | "Tablet Android 10" | — |
| m25 | 06:40:05 | BOT | "¡Buenas! Sí, tenemos Tablet Android 10..." | — |
| m26 | 06:40:33 | CLI | "Sí, quiero dos tablets" | Reitera cantidad 2 |
| m27 | 06:40:37 | BOT | "Para dejarte el pedido listo me falta la zona de entrega..." | **Pide dirección otra vez** |
| m28 | 06:41:24 | CLI | "Mariano, Calle Prueba 11, entre Calle A e Calle B, 55500000" | — |
| m29 | 06:41:27 | BOT | "Para dejarte el pedido listo me falta la dirección..." | **Ya la dio en m28** |
| m30 | 06:41:46 | CLI | "...es mi teléfono" | — |
| m31 | 06:41:51 | BOT | "Para dejarte el pedido listo me falta la dirección..." | **Ya la dio en m28/m30** |
| m32 | 06:44:47 | CLI | "la zona: Marianao, la dirección: Calle Calle Prueba, número 110 entre Calle A e Ins..." | Por fin da zona explícita |
| m33 | 06:44:51 | BOT | "Te confirmo: Tablet Android 10 x1 en 220 USD..." | **quantity=1 a pesar de pedir 2** |
| m34 | 06:45:42 | CLI | "Sí, pero que sean 2 tablets, quiero dos" | Corrige cantidad |
| m35 | 06:45:45 | BOT | "Tu pedido V-000001 quedó registrado." | **No crea venta nueva ni modifica qty** |
| m36 | 06:46:15 | TCP | "Recibido" | ACK correcto |
| m37 | 06:46:15 | BOT | "Confirmado, tu pedido V-000001 está en preparación." | — |
| m38 | 06:46:46 | TCP | "Entregado" | ACK correcto |
| m39 | 06:47:43 | TCP | "ENTREGADO V-000001" | ACK con código |
| m40 | 06:47:43 | BOT | "Entregado. ¡Gracias por confiar en Cuadra!" | — |

---

## 3. Bugs identificados en esta prueba

### B1. Repetición de plantillas idénticas
El bot envía el mismo texto literal en m3/m4, m6/m8, m19/m21/m23, m27/m29/m31. El cliente siente que habla con un grabador.
- **Causa:** No hay rotación de variantes ni control de repetición inmediata.

### B2. Pérdida de contexto entre turnos
Cuando el cliente da dirección (m18, m22, m28, m30), el siguiente mensaje del bot la ignora y pide "¿Dime cuál producto te interesa?" o "¿Me falta la dirección?" (m19, m23, m29, m31).
- **Causa:** El pipeline no mantiene el estado de captura de datos entre mensajes del mismo turno de compra. Cada mensaje se trata como aislado.

### B3. Cantidad ignorada
El cliente pide "dos tablets" en m16, m20, m26 y m34. La venta se crea con `quantity=1` (m33) y el bot no crea una segunda venta ni modifica la existente (m35).
- **Causa:** El NLU no extrae cantidad y el motor de ventas no soporta modificar qty de una venta abierta.

### B4. Escalación mal formulada (slot crudo)
El cliente pregunta por UCI (zona inactiva) en m9. El sistema escala con `datos_faltantes = ["ubicación exacta"]` y le envía al TCP una pregunta cruda. El TCP responde confuso (m11) y el bot devuelve esa respuesta al cliente literalmente (m12): "A q te refieres con ubicación exacta, pregunta nuestra dirección?"
- **Causa:** `datos_faltantes` se usa como pregunta cruda al TCP sin contexto ni formato.

### B5. Zona inactiva (UCI)
El cliente pregunta por "UCI" que es una zona `active=0` en el catálogo. El sistema no debería tratar eso como un dato faltante del proveedor, sino como una zona que no cubre deliveries.
- **Causa:** No hay validación de que la zona preguntada esté activa antes de escalar.

---

## 4. Qué SÍ funcionó

| Aspecto | Evidencia |
|---------|-----------|
| Ventas creadas y despachadas | V-000001 creada, despachada, confirmada, entregada |
| Acks TCP | `RECIBIDO` (m36) y `ENTREGADO V-000001` (m39) aplicados correctamente |
| No hay jobs fallidos | Todos los jobs COMPLETED |
| No hay errores de LLM | 100% de llamadas OK |
| Latencia aceptable | ~2,5–4,5 s por turno, sin cola de espera |

---

## 5. Solución propuesta

### P0 — Corregir antes del siguiente test

| # | Corrección | Archivo | Qué hace |
|---|-----------|---------|----------|
| 1 | **Rotación de plantillas** | `ai/prompts.py` + `ai/pipeline.py` | 3 variantes por plantilla clave (`ask_which_product`, `greeting_product`, `fallback_product`, `ask_client_data`), seleccionadas determinísticamente por conversación, sin repetir la anterior inmediatamente. |
| 2 | **Persistir estado de captura de datos** | `ai/pipeline.py` | Guardar en `context` los datos ya capturados (zona, dirección, teléfono, cantidad, producto) y no pedir lo que ya se tiene. Si el cliente da dirección, no volver a preguntar por producto. |
| 3 | **Extraer cantidad de compra** | `ai/llm.py` (`Intent`) + `ai/pipeline.py` | Añadir `cantidad` al esquema del NLU. Si el cliente dice "dos tablets", crear venta con `quantity=2`. Si ya hay venta abierta, permitir modificar qty o crear venta adicional. |
| 4 | **Escalación con contexto, no slot crudo** | `ai/pipeline.py:640-707` | La pregunta al TCP debe incluir: producto, texto literal del cliente, y qué dato falta específicamente. Nunca enviar `"ubicación exacta"` solo. |
| 5 | **Validar zona activa antes de escalar** | `ai/pipeline.py` + `domain/catalog.py` | Si el cliente pregunta por una zona inactiva (UCI), responder que no se entrega ahí, no escalar al TCP. |

### P1 — Calidad de conversación

| # | Corrección | Archivo |
|---|-----------|---------|
| 6 | **Ruta de queja/confusión** | `ai/pipeline.py` | Detectar cuando el cliente repite la misma petición 3+ veces y ofrecer hablar con un humano o cambiar de ángulo. |
| 7 | **No resetear contexto en medio de compra** | `ai/pipeline.py` | Si hay una venta en `CAPTURE_DATA`, un saludo o "¿qué producto?" no debe reiniciar el flujo. |
| 8 | **Validar que la venta creada coincida con lo pedido** | `ai/pipeline.py` | Antes de crear la venta, confirmar con el cliente: "¿Quiero decir 2 tablets a $220 USD cada una? ¿Confirmas?" |

### P2 — Métricas y observabilidad

| # | Corrección | Archivo |
|---|-----------|---------|
| 9 | **Contar repeticiones de plantilla** | `ai/events.py` | Loggear cuando se usa la misma variante dos turnos seguidos. |
| 10 | **Rastrear cantidad pedida vs. creada** | `events.py` | Loggear `requested_qty` y `created_qty` para detectar discrepancias. |

---

## 6. Orden de implementación recomendado

1. **B1 + B2** (rotación + contexto) — es lo que más molestó al cliente en esta prueba.
2. **B3** (cantidad) — el cliente pidió 2 y recibió 1.
3. **B4 + B5** (escalación) — la escalación a UCI fue confusa para ambos lados.
4. **P1** (queja/contexto) — para evitar bucles en futuras pruebas.

¿Quieres que implemente primero la rotación de plantillas y la persistencia de contexto (B1+B2), o prefieres atacar la cantidad (B3) que fue el error más grave para el cliente?
