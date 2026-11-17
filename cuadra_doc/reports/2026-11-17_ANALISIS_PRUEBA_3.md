# Análisis de prueba #3 — ciclo completo de venta

**Fecha:** 2026-11-17
**Entorno:** BD, logs y backend reiniciados desde cero. Trazado en `ALL`.
**Datos:** 1 conversación de cliente, 1 de TCP, 14 turnos, 39 mensajes, **1 venta liquidada**.
**Fuentes:** `messages`, `escalations`, `sales`, `traces` (97 filas), `llm_calls` (23), `cuadra.log`.

---

## 1. Resumen ejecutivo

| Métrica | Valor |
|---|---|
| Turnos del cliente | 14 |
| Mensajes totales | 39 (24 cliente/bot + 15 TCP) |
| Trazas `pipeline.handle_turn` | 14, **0 fallos** |
| Latencia del turno p50 / media / max | 3,0 s / **3,3 s** / 6,1 s |
| `intent` del LLM | 14 llamadas, media **2,3 s** |
| `compose` | 3 llamadas, media 3,2 s |
| `tcp_extraction` | 5 llamadas, media 4,3 s |
| Ventas creadas | **1** (`V-000001`, 1100 USD, La Lisa) |
| Comisión | 60 USD, liquidada |
| Escalaciones | 3 (E-0001, E-0002, E-0003), todas resueltas |
| Errores / excepciones | 0 |

**Diagnóstico en una frase:** el flujo funcional ya se sostiene de punta a punta, pero la **trazabilidad entre los dos chats** es frágil: el bot no cita la referencia al TCP, acepta respuestas por parecido y reenvía al cliente lo que el TCP escribe, incluida una queja dirigida al propio bot.

---

## 2. Timeline real

### 2.1 Conversación del cliente (`c1`, @rolandoepd)

| # | Cliente | Bot | Veredicto |
|---|---|---|---|
| 1 | Hola buenas noches | Saludo y pregunta qué producto | Correcto |
| 3 | tienes laptops | Lista las 3 laptops con precio | Correcto |
| 5 | me interesa la gamer | Ficha del producto (1100 USD) | Correcto |
| 7 | Soy de la UCI, haces domicilio ahi | Ficha genérica del producto | **F9**: no valida la zona (UCI `active=0`) |
| 9 | Si, que microprocesador tiene esa laptop | «la GPU RTX 4060 es un microprocesador» | **F1**: spec inventada por `compose` |
| 11 | Te pregunto por el microprocesador no por la GPU | Escala al almacén | Correcto |
| 13 | Si | **Repite** el mensaje de escalación | **F5**: «Si» no se interpreta como «sí, espera» |
| 17 | Si (tras la respuesta del almacén) | Re-cotiza el producto | **F5**: «Si» tras oferta no inicia la venta |
| 19 | Vale, cuando me la traes | Escala «fecha de entrega» | **F6**: escala antes de que exista pedido |
| 23 | Si reservamela | Pide zona, dirección y teléfono | Correcto (aunque llega 4 minutos tarde) |
| 25 | La lisa, calle 4 … numero 20, 55645455 | Confirmación del pedido | **F10**: pierde «numero 20» |
| 27 | si | `V-000001` registrado | Correcto |
| 31 | vale, gracias | «¿Te la traigo en 48h?» y re-cotiza | **F8**: post-venta inventa y re-cotiza |
| 33 | si | Vuelve a cotizar | **F8** |
| — | — | «Entregado. ¡Gracias por confiar en Cuadra!» | Correcto (ya con el arreglo de acks) |

### 2.2 Conversación del TCP (`c2`, @rolandopl)

| # | TCP | Efecto |
|---|---|---|
| 15 | es un i5 1250 H | Resuelve E-0001 y E-0002 a la vez (**F4**: duplicadas) |
| 21 | «No me has dado los datos de la venta…» | Se guarda como `dato_proveedor` del producto 13 (**F2**) |
| 22 | — | El bot **reenvía literal** esa queja al cliente (**F3**) |
| 29 | Recibido | `V-000001` → `CONFIRMED`, avisa al cliente |
| 35 | Entregado | Correlacionado con una escalación cualquiera (**F7**, corregido) |
| 36 | ENTREGSDO | Sin efecto (typo) |
| 37 | ENTREGADO | Sin efecto |
| 38 | V-000001 entregado | `DELIVERED` → `SETTLED` |

---

## 3. Fallas identificadas

### F1 — CRÍTICO: el compositor inventa especificaciones

**Evidencia:** mensaje 10, «La laptop gamer RTX 4060 incluye la GPU RTX 4060, que es un microprocesador especializado para gaming».

**Causa:** `_compose_open` recibe el producto en JSON y redacta con el modelo; `guardrails.validate_reply` no comprueba que los datos técnicos existan en `specifications_json`. El modelo rellenó el hueco «microprocesador» con la GPU porque el campo estaba vacío.

**Arreglo propuesto:** guardrail de specs — toda afirmación técnica debe salir de `specifications_json`; si el campo no existe, el compositor no puede responder (fallback: escalar). Es el fallo de mayor riesgo reputacional: un dato inventado sobre hardware es una devolución garantizada.

**Estado:** pendiente.

---

### F2 — El mensaje del TCP contamina el catálogo

**Evidencia:** `products.specifications_json` del producto 13:

```json
{"dato_proveedor":"No me has dado los datos de la venta, no te puedo confirmar, si no tengo ni la direccion del cliente"}
```

**Causa:** `tcp_extractor` interpretó cualquier respuesta del TCP como dato de producto. El TCP no estaba respondiendo una consulta: estaba reclamando los datos de la venta.

**Arreglo propuesto:** el extractor solo corre si el mensaje está correlacionado con una escalación **abierta** y la respuesta no es una queja/negativa sobre el proceso. Validar que el texto extraído no sea una frase de rechazo.

**Estado:** pendiente. Limpiar además el `specifications_json` contaminado del producto 13.

---

### F3 — El bot reenvía literal la respuesta del TCP

**Evidencia:** mensaje 22, «¡Buenas! Ya me confirmó el almacén: No me has dado los datos de la venta, no te puedo confirmar…».

**Causa:** `tcp_triage._customer_answers` prioriza la respuesta completa del TCP; el filtro de moneda no cubre una queja.

**Arreglo propuesto:** nunca reenviar literal un mensaje del TCP al cliente. La respuesta al cliente se compone desde el resumen validado (`escalation_resolved`) y, si no hay resumen útil, se usa la plantilla genérica. El texto crudo del TCP es material interno, no copy de cliente.

**Estado:** pendiente.

---

### F4 — Escalaciones duplicadas

**Evidencia:** E-0001 (04:49:36) y E-0002 (04:49:54) con la misma pregunta («microprocesador») para el mismo producto y conversación. Una sola respuesta del TCP las resolvió a las dos.

**Causa:** el dedupe no cubre la ventana entre el encolado de `send_escalation` y su envío; dos turnos seguidos crearon dos filas.

**Arreglo propuesto:** clave de dedupe `(conversation_id, product_id, normalised_question)` sobre escalaciones abiertas, aplicada en el momento de crear la escalación (no al enviarla).

**Estado:** pendiente.

---

### F5 — «Si» no se interpreta contra la oferta pendiente

**Evidencia:** mensajes 13, 17 y 33: el cliente responde «Si» a una pregunta del bot y el bot no recuerda qué preguntó.

**Causa:** la etapa se guarda en el contexto, pero no la **oferta pendiente** (producto + pregunta). Un «Si» aislado no tiene antecedente contra el que resolverse.

**Arreglo propuesto:** persistir `pending_offer` (tipo de oferta, producto, venta) en el contexto y resolver los afirmativos/negativos contra ella antes de pasar por el NLU.

**Estado:** pendiente.

---

### F6 — Se escala «fecha de entrega» antes de existir pedido

**Evidencia:** E-0003 (04:52:09), cuando el cliente aún no había dado dirección ni teléfono.

**Causa:** la pregunta de entrega se clasifica como `consulta_dato` sin comprobar la etapa. La fecha depende del pedido (zona + dirección), así que preguntarla antes es una promesa que el TCP no puede cumplir.

**Arreglo propuesto:** las consultas de logística (`fecha de entrega`, `cuándo llega`, `envío`) solo se escalan cuando hay una venta en `DISPATCHED` o posterior; antes de eso se responde «se coordina al confirmar la entrega».

**Estado:** pendiente.

---

### F7 — CORREGIDO: un ack se correlacionaba con una escalación

**Evidencia:** `Entregado` (mensaje 35) no encontró la venta (estaba `CONFIRMED`, el ack solo buscaba `DISPATCHED`) y cayó en la correlación de escalaciones. E-0003 se marcó resuelta con «Entregado» como respuesta.

**Arreglo aplicado:** protocolo de IDs y acks (ver §4). `ENTREGADO` acepta `DISPATCHED` o `CONFIRMED`; un ack sin venta genera `tcp_ack_unmatched` y nunca toca una escalación; con varias ventas abiertas y sin `V-######` se pide la referencia.

**Estado:** hecho, con 10 tests nuevos.

---

### F8 — La etapa `DONE` inventa y re-cotiza

**Evidencia:** mensaje 32, «…1100 USD. ¿Te la traigo en 48h?», y mensaje 34, re-cotización tras la venta.

**Causa:** `DONE` no tiene plantilla propia: cae en el compositor libre y en el flujo de cotización.

**Arreglo propuesto:** etapa post-venta cerrada: solo seguimiento del pedido (`V-000001`), sin precio ni plazos inventados. «48h» es exactamente el tipo de promesa que no se puede hacer sin el TCP.

**Estado:** pendiente.

---

### F9 — La zona no se valida contra cobertura

**Evidencia:** el cliente dice «Soy de la UCI»; `zones.id=16` («UCI») existe con `active=0` y no está en `supplier_delivery_zones`. El bot respondió con la ficha del producto y siguió adelante.

**Causa:** la zona mencionada en lenguaje libre no se resuelve a `zones.id` en el turno de descubrimiento; solo se valida al crear la venta.

**Arreglo propuesto:** resolver zona en el turno en que se menciona y responder cobertura/no cobertura antes de seguir cotizando. Si la zona no tiene cobertura, no se debe llegar a pedir dirección y teléfono.

**Estado:** pendiente.

---

### F10 — La dirección pierde el número

**Evidencia:** el cliente escribe «calle 4 entre independencia y calixto, numero 20»; se guarda «calle 4 entre independencia y calixto».

**Causa:** el extractor de dirección corta en la primera coma y descarta el segmento «numero 20».

**Arreglo propuesto:** conservar el número de casa como parte de la dirección (o campo aparte) y exigirlo en la confirmación al cliente, que es donde el cliente puede corregir.

**Estado:** pendiente.

---

### F11 — Las salidas manuales del userbot no se registran

**Evidencia:** durante la prueba se escribió desde el userbot a la cuenta personal («Si, reservamela») y luego se reenvió desde la personal. El sistema no registra los mensajes que el operador envía a mano desde el userbot.

**Causa:** el listener del userbot solo persiste mensajes de peers identificados como TCP.

**Arreglo propuesto:** registrar toda salida del userbot en `messages` con `author='operator'`, aunque el destino no sea un TCP. Sin esto, el historial no explica lo que ve el cliente y cualquier auditoría futura es incompleta.

**Estado:** pendiente. No hubo daño en datos: el mensaje no se persistió y la venta se creó correctamente por el flujo normal.

---

## 4. Lo que ya quedó arreglado en este bloque

Protocolo de referencias y acks (decisión §10 de `00_decisiones.md`):

- `sales.detect_ack` público, con tolerancia a erratas solo en mensajes de una palabra.
- `_ACK_FROM_STATUSES`: cada ack mueve estados concretos (`ENTREGADO` desde `DISPATCHED`/`CONFIRMED`).
- Ack repetido → `DUPLICATE`, sin reabrir la venta ni marcar `NEEDS_OPERATOR`.
- Ack sin venta → `tcp_ack_unmatched`; nunca cae en la correlación de escalaciones.
- Varias ventas abiertas sin código → `AMBIGUOUS` + `ask_tcp_reference`.
- Varias escalaciones abiertas sin `[E-####]` → `tcp_reply_ambiguous` + `ask_tcp_reference`.
- `tcp_order` y `escalation_question` piden la referencia en la respuesta.
- Suite: **320 tests OK**.

---

## 5. Criterios de aceptación del re-test #4

| # | Criterio | Cómo se comprueba |
|---|---|---|
| A1 | Ninguna respuesta del bot afirma un dato técnico que no esté en `specifications_json` | Revisar mensajes + `traces` de `compose`; el campo vacío debe escalar, no inventar |
| A2 | Ningún mensaje del TCP se reenvía literal al cliente | Comparar cuerpos de `messages` OUT de `c1` contra los IN de `c2` |
| A3 | Un `specifications_json` solo cambia si el mensaje del TCP responde a una escalación abierta y no es una queja | Diff del catálogo antes/después + `catalog_mutations` |
| A4 | Dos turnos seguidos con la misma pregunta generan **una** escalación | `SELECT COUNT(*) FROM escalations WHERE conversation_id=? AND product_id=?` |
| A5 | «Si» tras una oferta ejecuta la oferta (reserva, confirmación o espera) | Timeline: cada «Si» tiene una acción distinta |
| A6 | No se escala logística antes de que exista venta despachada | `escalations` sin venta asociada en `DISPATCHED` |
| A7 | `ENTREGADO` repetido no produce mensaje al cliente ni `NEEDS_OPERATOR` | `events` con `tcp_sale_ack_duplicate`, 0 OUT nuevos |
| A8 | Con dos ventas abiertas, un ack sin código pide la referencia y no mueve ninguna | `ask_tcp_reference` en `jobs`, ventas intactas |
| A9 | La etapa `DONE` no menciona precios ni plazos no confirmados | Mensajes post-`V-000001` |
| A10 | Zona sin cobertura se rechaza antes de pedir dirección | Conversación con «UCI» |
| A11 | La dirección guardada conserva el número de casa | `sales.delivery_address` vs. mensaje del cliente |
| A12 | Latencia media del turno ≤ 3,5 s | `traces` de `pipeline.handle_turn` |

**Guion sugerido para el re-test:** mismo recorrido que la prueba #3 (catálogo → ficha → zona sin cobertura → spec inexistente → «Si» tras oferta → reserva → acks con y sin código → post-venta), ejecutado primero por el operador y solo después por terceros.
