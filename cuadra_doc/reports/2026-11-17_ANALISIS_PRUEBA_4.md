# Análisis de prueba #4 — conversación, stock y especificaciones

**Fecha:** 2026-11-17
**Entorno:** servicio en marcha, BD real, trazado en `ALL`.
**Datos:** 1 conversación de cliente (22 entrantes, 25 salientes, 22 turnos), 1 de TCP, **1 venta liquidada** (`V-000001`), 1 escalación resuelta, 170 trazas.
**Fuentes:** `messages`, `turns`, `sales`, `escalations`, `traces`, `events`.

---

## 1. Resumen ejecutivo

| Métrica | Valor |
|---|---|
| Turnos del cliente | 22 |
| Mensajes del cliente / bot | 22 / 25 |
| Trazas `pipeline.handle_turn` | 22, **0 fallos** |
| Latencia del turno media / max | **2,56 s** / 5,13 s |
| `intent` del LLM | 20 llamadas, media **2,15 s** |
| `compose` | 2 llamadas (2,8 s y 3,0 s) |
| Ventas creadas | **1** (`V-000001`, 1 × 1100 USD, La Lisa) |
| Escalaciones | 1 (`E-0001`, procesador), resuelta |
| Errores / excepciones | 0 |

**Diagnóstico en una frase:** el ciclo de venta y los acks funcionaron, pero la conversación sonó a robot (saludo en cada mensaje, «gracias» respondido con el pedido, «se nos fue» ante una cantidad que sí existía) y el compositor **inventó especificaciones** porque el guardrail de specs no recibía las especificaciones del producto.

---

## 2. Timeline real (cliente, @cliente_demo)

| # | Cliente | Bot | Veredicto |
|---|---|---|---|
| 1-2 | Hola / buens tardes | Dos saludos seguidos | **F13**: no reconoce la errata y saluda dos veces |
| 5 | Ando buscando una laptop | Cotiza «Laptop Empresarial» | Aceptable, pero convenía listar |
| 7 | Que ofertas de laptops tienes | Cotiza una laptop | **F13**: ignora que pide el listado |
| 9 | No tienes mas ningun tipo q no sea la empresaial | Repite la misma laptop | **F13**: no reconoce la petición de alternativas |
| 11 | Que otra laptops tienes | Lista las 3 laptops | Correcto |
| 13 | me interesa la gamer, que propiedades tiene | «pantalla gaming, RTX 4060, **16 GB de RAM y 1 TB de SSD**» | **F16**: inventa specs (solo había «i5 1250 H») |
| 15 | Que microprocesador tieen | Escala al almacén | Correcto, pero con tema duplicado (**F15**) |
| 19 | Vale, quiero 5 de esas | «Son 5500 USD por 5…» pide datos | Correcto |
| 21 | soy de la UCI, calle 45…, 55500001 | «Me falta la zona de entrega» | **F9**: no resuelve «soy de la UCI» |
| 23 | La zona de entrega es la UCI | Rechaza la zona (no cubierta) | Correcto |
| 25 | puedo recibirla en la Lisa tambien | Re-cotiza y **pierde las 5 unidades** | **F14**: la cantidad no sobrevive |
| 27 | que sean 15 entonces | Confirma 15 × 1100 USD | Correcto |
| 29 | Si | «Ese se nos fue» + monitores y tablets | **F14**: había 1 unidad; alternativas de otra categoría |
| 31 | no te quedan de esas laptops | Re-cotiza el producto | **F14**: no responde la duda |
| 33 | cuantas puedo comprar | Re-cotiza otra vez | **F14**: no responde la cantidad |
| 35 | laptops gaming, que cantidad tienes en stock | Lista las 3 laptops | **F14**: no resuelve «categoría + calificador» |
| 37 | Quiero 2 laptops gaming | Confirma 2 unidades | Correcto (pero no había 2) |
| 39 | Si | «Ese se nos fue» + monitores y tablets | **F14**: repetido |
| 41-43 | Dame una laptop gaming / Si, quiero una | `V-000001` registrado | Correcto |
| 47 | gracias | **Repite** «Tu pedido V-000001 quedó registrado» | **F12**: un banal no es una compra |

### TCP (@proveedor_tres)

| # | TCP | Efecto |
|---|---|---|
| 17 | tiene un i5 1250 H | Resuelve `E-0001` y responde al cliente |
| 45 | RECIBIDO V-000001 | `DISPATCHED` → `CONFIRMED`, avisa al cliente |
| 49 | ENTREGADO V-000001 | `DELIVERED` → `SETTLED` |

Los acks citaron la venta y movieron los estados correctos: **sin cambios**.

---

## 3. Fallas y correcciones

### F12 — CORREGIDO: «gracias» recibía la respuesta del pedido

**Evidencia:** mensaje 47 «gracias» → mensaje 48 «Tu pedido V-000001 quedó registrado…».

**Causa:** el NLU clasificó «gracias» como `comprar` y la máquina de venta repitió la confirmación.

**Arreglo (decisión §14):**
- `pipeline._banal_kind` reconoce `thanks` / `sorry` / `ack` **solo si todo el mensaje es banal**; «gracias, ¿y el precio?» sigue su ruta normal.
- `pipeline._banal_outcome` responde con `banal_thanks` / `banal_sorry` / `banal_ack`, **no cambia de etapa** y no se aplica en `CAPTURE_DATA`/`CONFIRM`.
- Fast path en `_run_turn`: un saludo o un banal no llama al NLU (ahorra ~2,1 s y evita la mala clasificación).

### F13 — CORREGIDO: saludo en cada respuesta y erratas de saludo

**Evidencia:** «¡Buenas!» en los mensajes 6, 8, 10, 26, 32, 34, 42; dos saludos al inicio; «buens tardes» no se reconoce.

**Arreglo (decisión §14):**
- `_GREETING` acepta `buens`, `bueas`, `buenas`, `buenos` y sus variantes con «tardes/noches/días».
- `_should_greet` / `_mark_greeting` / `_with_greeting` con `_GREETING_COOLDOWN_S = 600`: solo se saluda al inicio o tras 10 minutos; dentro del cooldown un saludo recibe `greeting_again` sin «Buenas».
- Se eliminaron los saludos fijos de `greeting_product`, `fallback_product`, `escalation_*`, `mutation_*` y `missing_data_escalate`.
- `_LIST_REQUEST` reconoce «ofertas», «muéstrame», «qué … tienes/hay/venden»; `_ALTERNATIVES` reconoce «ningún tipo», «no sea», «más tipo».

### F14 — CORREGIDO: cantidad mayor que el stock

**Evidencia:** pedido de 5 y de 15 unidades con 1 en stock → «Ese se nos fue por ahora. Tengo Monitor LED 24… y Tablet Android 10…».

**Arreglo (decisión §14):**
- `_available_stock` descuenta `stock_reserved` y `_sale_stock_outcome` responde «solo me queda(n) N» con el nombre real; nunca «se nos fue» ni productos de otra categoría.
- Pre-check `qty > available` **antes** de pedir datos o confirmar; se conserva `sale_qty = available` para que el siguiente «Si» cree la venta con esa cantidad.
- `_stock_outcome` responde preguntas de cantidad (`_STOCK_QUESTION`) con el stock real, no con una cotización ni un listado.
- `catalog.siblings` (misma familia) sustituye a `alternatives` en agotados y alternativas.
- `catalog.narrow_products` resuelve «laptops gaming» → `Laptop Gamer RTX 4060` (prefijo de 3 letras o ratio ≥ 0,7).

### F15 — CORREGIDO: tema de escalación duplicado

**Evidencia:** la pregunta al TCP incluía «microprocesador, procesador».

**Arreglo:** `_canonical_topic` mapea el dato al tópico de `guardrails.SPEC_TOPICS` y `_dedupe` elimina repetidos antes de crear la escalación.

### F16 — CORREGIDO: el compositor inventó especificaciones

**Evidencia:** mensaje 14: «…16 GB de RAM y 1 TB de SSD» cuando el catálogo solo tenía `{"dato_proveedor": "tiene un i5 1250 H"}`.

**Causa raíz:** `catalog.find_product` / `search_products` devuelven la fila SQL cruda, que trae `specifications_json` pero **no** la clave `specifications`. `guardrails._specs_corpus` exige `product["specifications"]` como dict; al no encontrarla devolvía `None` y `check_specs` se desactivaba. Por eso el texto inventado pasó el guardrail y se envió (`generator: "llm"`).

**Arreglo:**
- `pipeline._guard_product` normaliza cada producto del contexto de guardrails (parsea `specifications_json` → `specifications`) y `_guard_context` lo aplica al producto, candidatos y alternativas.
- Con esto `check_specs` vuelve a rechazar (razón `spec`) cualquier especificación que no esté en el catálogo, y el turno cae al fallback determinista.
- Test de regresión: una fila de `find_product` expone sus specs y una pantalla de 15" inventada se rechaza.

**Nota:** el guardrail es por tópico. Una especificación guardada bajo la clave genérica `dato_proveedor` no autoriza a afirmar el tópico («procesador») en una respuesta compuesta; en ese caso el cliente recibe la plantilla segura. Las respuestas del TCP siguen llegando al cliente por `escalation_resolved_*`, que valida con un contexto sin producto y no bloquea las palabras del proveedor.

### Otros ajustes menores

- `sale_ask_data` (variante 2) ya no dice «¿Me los pasas?» cuando falta un solo dato: «¿Me pasas el dato?».
- «Ando buscando una laptop» sigue cotizando la laptop más barata; convertirlo en listado cuando no hay calificador es una decisión de producto pendiente (no se cambió para no alargar la respuesta).

---

## 4. Lo que ya funcionó

- **Protocolo de IDs y acks:** `RECIBIDO`/`ENTREGADO V-000001` movieron la venta y avisaron al cliente.
- **Zona sin cobertura:** «soy de la UCI» se rechazó en el turno correcto (UCI inactiva) antes de pedir dirección.
- **Escalación única:** una sola `E-0001`, resuelta, sin duplicados en el TCP.
- **Venta:** 1 unidad, 1100 USD, La Lisa, dirección y teléfono completos, `SETTLED`.
- **Sin excepciones** en 22 turnos.

---

## 5. Verificación

- `ConversationPolishTests`: 15 tests nuevos (banales, saludos, stock, alternativas, dedupe de temas, zona, specs).
- Suite completa: **367 tests OK**.
- Comprobado contra la BD real: «laptops gaming» → `Laptop Gamer RTX 4060`; «soy de la UCI» → zona `UCI`; `_banal_kind` y `_is_greeting` con las frases del test.

---

## 6. Pendientes / siguiente prueba

1. Repetir la prueba con el servicio reiniciado (el test #4 corrió con el código anterior a F12-F16).
2. Probar: «gracias» tras la venta, «quiero 5» con 1 en stock, «cuántas quedan», «laptops gaming», «buens tardes» dos veces.
3. Decidir si el debounce debe agrupar mensajes separados ~2 s (hoy generan dos turnos y dos respuestas).
4. Revisar el uso de la clave genérica `dato_proveedor` al guardar respuestas del TCP: guardar el tópico canónico permitiría al compositor responder sin caer al fallback.
