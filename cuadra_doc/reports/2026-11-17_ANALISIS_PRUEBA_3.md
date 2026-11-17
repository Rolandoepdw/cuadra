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

### F1 — CORREGIDO: el compositor inventa especificaciones

**Evidencia:** mensaje 10, «La laptop gamer RTX 4060 incluye la GPU RTX 4060, que es un microprocesador especializado para gaming».

**Causa:** `_compose_open` recibe el producto en JSON y redacta con el modelo; `guardrails.validate_reply` no comprobaba que los datos técnicos existieran en `specifications_json`. El modelo rellenó el hueco «microprocesador» con la GPU porque el campo estaba vacío.

**Arreglo aplicado (decisión §11):**

- `pipeline._missing_spec_topics` detecta por léxico el tema técnico preguntado (procesador, GPU, RAM, almacenamiento, pantalla, batería, cámara, garantía). Si no está en `specifications`, la respuesta escala al TCP **sin llamar al compositor**.
- `guardrails.check_specs` rechaza (razón `spec`) cualquier respuesta compuesta que afirme un dato técnico ausente del catálogo. Las negaciones honestas no se bloquean.

**Estado:** hecho, con 6 tests nuevos.

---

### F2 — CORREGIDO: el mensaje del TCP contamina el catálogo

**Evidencia:** `products.specifications_json` del producto 13:

```json
{"dato_proveedor":"No me has dado los datos de la venta, no te puedo confirmar, si no tengo ni la direccion del cliente"}
```

**Causa:** `tcp_extractor` interpretó cualquier respuesta del TCP como dato de producto. El TCP no estaba respondiendo una consulta: estaba reclamando los datos de la venta.

**Arreglo aplicado (decisión §11):** `tcp_triage.looks_like_answer` reconoce quejas/negativas de proceso; `tcp_process` las registra como `tcp_not_an_answer` y **no** llama al extractor, no guarda la especificación y deja la consulta abierta. «No tengo unidades» sigue siendo una respuesta válida.

**Datos reparados:** `scripts/clean_specs.py` eliminó el `dato_proveedor` contaminado del producto 13 (queda `{}`).

**Estado:** hecho, con 3 tests nuevos.

---

### F3 — CORREGIDO: el bot reenvía literal la respuesta del TCP

**Evidencia:** mensaje 22, «¡Buenas! Ya me confirmó el almacén: No me has dado los datos de la venta, no te puedo confirmar…».

**Causa:** `tcp_triage._customer_answers` prioriza la respuesta completa del TCP; el filtro de moneda no cubre una queja.

**Arreglo aplicado (decisión §11):** `_customer_answers` descarta las quejas (`looks_like_answer`) tanto en la respuesta cruda como en el resumen. Si no queda ninguna utilizable, el cliente recibe `escalation_unconfirmed` («sigo esperando que el almacén me confirme ese dato»), nunca una disponibilidad inventada. La respuesta completa del TCP se sigue usando cuando **sí** es una respuesta (H-22 intacto).

**Estado:** hecho, con 2 tests nuevos.

---

### F4 — Escalaciones duplicadas

**Evidencia:** E-0001 (04:49:36) y E-0002 (04:49:54) con la misma pregunta («microprocesador») para el mismo producto y conversación. Una sola respuesta del TCP las resolvió a las dos.

**Causa:** el dedupe no cubre la ventana entre el encolado de `send_escalation` y su envío; dos turnos seguidos crearon dos filas.

**Arreglo aplicado (decisión §12):** `escalations.create` normaliza la pregunta y reutiliza la escalación `PENDING` idéntica; devuelve `(escalation, created)` y solo una fila nueva encola `send_escalation`. La reutilización queda como evento `escalation_reused`.

**Estado:** hecho, con 1 test nuevo (dos turnos iguales → 1 escalación, 1 job).

---

### F5 — «Si» no se interpreta contra la oferta pendiente

**Evidencia:** mensajes 13, 17 y 33: el cliente responde «Si» a una pregunta del bot y el bot no recuerda qué preguntó.

**Causa:** la etapa se guarda en el contexto, pero no la **oferta pendiente** (producto + pregunta). Un «Si» aislado no tiene antecedente contra el que resolverse.

**Arreglo aplicado (decisión §12):** `cuadra.ai.offers` persiste `pending_offer` (tipo, producto) en el contexto y la consume el turno siguiente. Un «Si»/«No» desnudo se resuelve contra la oferta antes de `_maybe_handle_sale`; una pregunta no la ejecuta. `tcp_triage.resume_escalations` deja la oferta de reserva al responder al cliente.

**Estado:** hecho, con 6 tests nuevos (sí inicia venta, no declina, la oferta no sobrevive a una pregunta, clasificación de respuestas y detección de la pregunta).

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

Especificaciones y mensajes del TCP (decisión §11):

- `pipeline._missing_spec_topics` escala los temas técnicos que el catálogo no tiene, sin llamar al compositor.
- `guardrails.check_specs` bloquea especificaciones inventadas (razón `spec`).
- `tcp_triage.looks_like_answer` distingue una queja de una respuesta; `tcp_process` registra `tcp_not_an_answer` y no contamina el catálogo.
- `_customer_answers` no reenvía quejas; el último recurso es `escalation_unconfirmed`.
- `scripts/clean_specs.py` reparó el `specifications_json` del producto 13.

Suite: **338 tests OK**.

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
