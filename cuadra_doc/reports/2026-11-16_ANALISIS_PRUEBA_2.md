# Análisis de prueba #2 — conversación real con el agente

**Fecha:** 2026-11-16
**Entorno:** BD, logs y backend reiniciados desde cero. Trazado en `ALL`.
**Datos:** 1 conversación, 10 turnos, 20 mensajes, **0 ventas**.
**Fuentes:** `messages`, `conversation_facts`, `customers`, `traces` (80 filas), `llm_calls` (10), `cuadra.log`.

---

## 1. Resumen ejecutivo

| Métrica | Valor |
|---|---|
| Turnos | 10 |
| Mensajes del bot | 10 |
| Respuestas **idénticas** | 6 de 10 (`ask_which_product`) |
| Ventas creadas | **0** |
| Latencia total p50 / p95 / max | 3,1 s / 4,3 s / 5,1 s |
| `intent_ms` p50 / max | 2,9 s / 4,3 s (93 % del turno) |
| `compose_ms` | 0 en 10/10 (nunca se usó el compositor) |
| Escalaciones | 0 |
| Errores / excepciones | 0 |

**Diagnóstico en una frase:** el contexto **sí** se persiste (la hipótesis C2 del análisis anterior era incorrecta); lo que falla es que el flujo **lo descarta** en cuanto el NLU no repite el nombre del producto, y la máquina de estados reinicia la venta.

---

## 2. Timeline real

| # | Cliente | Bot | Veredicto |
|---|---------|-----|-----------|
| m1 | Hola | "Dime cuál producto te interesa…" | Aceptable |
| m3 | buenas noches | **la misma frase** | Falla: no saluda, repite |
| m5 | Ando en busca de laptops, que ofertas tienes | **la misma frase** | Falla: no lista laptops |
| m7 | Que laptops tienes a la venta | **la misma frase** | Falla: 3ª vez |
| m9 | Quiero 3 Laptop Empresarial, que precio tienen | pide zona/dirección/teléfono | Falla: **no dice el precio** |
| m11 | Soy de Playa, mi dirección es Calle Prueba 110… | pide teléfono | Correcto |
| m13 | 55500000 | "Te confirmo: … **x1** en 350 USD…" | Falla: **ignora cantidad 3** |
| m15 | Si, pero quiero 3 no 1 | **"Dime cuál producto te interesa"** | **Falla crítica: se pierde la venta** |
| m17 | Dame un listado de laptops que tengas en venta | "Sí, tenemos Laptop Empresarial, 350 USD" | Correcto (a la 4ª) |
| m19 | y no tiene otro tipo de laptop? | **"Dime cuál producto te interesa"** | Falla: incoherente |

---

## 3. Fallas identificadas

### F1 — CRÍTICO: la venta se pierde en la confirmación

m15 «Si, pero quiero 3 no 1» → stage pasa de `CONFIRM` a `DISCOVERY` y la venta desaparece.

**Evidencia (traces #61 y #62):**

```
#61 pipeline._resolve_product  IN {intent: comprar, producto_solicitado: "null", context_stage: "CONFIRM"}
                              OUT {product_id: null}
#62 pipeline.handle_turn      OUT {stage: "DISCOVERY", sale_id: null}
```

**Cadena de causa:**

1. El NLU devolvió `"producto": "null"` — un **string**, no `null`.
2. `Intent.producto: str | None` (`ai/llm.py:66`) no lo normaliza → `"null"` es *truthy*.
3. `_resolve_product` entra en la rama H-10 (`pipeline.py:166-171`): «nombró un producto que no está en el catálogo → no hay producto».
4. `_maybe_handle_sale` corta en seco: `if product is None: return None` (`pipeline.py:422`).
5. `_stage_for` resetea: `if product is None: return "DISCOVERY"` (`pipeline.py:550`).

**Arreglo:**

- Normalizar en `Intent` (`"null"`, `"none"`, `""`, `"ninguno"` → `None`).
- En `_maybe_handle_sale`, si `product is None` y hay `context.product_id` o venta abierta, usar ese producto. Mantener H-10 solo cuando el cliente nombró un producto real que no existe.

---

### F2 — CRÍTICO: la cantidad se descarta

m9 pide 3, la confirmación (m14) dice x1 y no se crea venta.

**Causa:** `qty` se calcula solo con el intent del turno actual (`pipeline.py:433`). El campo `qty_solicitada` se emite en el outcome (`pipeline.py:543`) y **nadie lo lee ni lo persiste** (única aparición en todo el backend). En el turno del teléfono `intent.cantidad` es `null` → `qty = 1`.

**Arreglo:** guardar la cantidad en el contexto (o en la venta abierta) al capturarla, y usarla en `sale_confirm` y en `create_sale`.

---

### F3 — CRÍTICO: las consultas por categoría no resuelven

«laptops», «laptops que tengas en venta» (m5, m7) → plantilla genérica. Solo resolvió cuando dijo «Dame un listado de laptops…».

**Causa:** `_resolve_product` (`pipeline.py:135-177`) busca por nombre de producto. «laptops» (plural/categoría) no coincide con «Laptop Empresarial»; el fallback semántico exige respaldo léxico y tampoco pasa.

**Arreglo:** resolución por **categoría** → devolver lista corta (nombre + precio) y preguntar cuál. Es lo que un cliente espera al preguntar «¿qué laptops tienes?».

---

### F3b — ALTO: salta a capturar datos sin cotizar

m9 pregunta precio y recibe «me falta la zona, la dirección y un teléfono». El precio no aparece hasta m18.

**Causa:** `_maybe_handle_sale` se ejecuta antes que `_decide` y, con `intent == comprar`, va directo a `_missing_slots` (`pipeline.py:494-508`).

**Arreglo:** cotizar primero (precio unitario y total por cantidad), y pedir los datos en el mismo mensaje o en el siguiente.

---

### F4 — ALTO: `consulta_dato` con producto resuelto pregunta «¿cuál producto?»

m19 «¿y no tiene otro tipo de laptop?» → plantilla genérica, aunque `product_id=1` estaba resuelto (trace #77).

**Causa:** `pipeline.py:694-706` usa `prompts.TEMPLATES["ask_which_product"]` (texto fijo, sin variantes) cuando no hay datos del proveedor.

**Arreglo:** si hay producto y la pregunta es «¿otro modelo/tipo?», listar alternativas de la misma categoría o decir que es el único disponible.

---

### F5 — ALTO: las variantes anti-repetición nunca rotan

6 respuestas idénticas seguidas. Existen 3 variantes de `ask_which_product`, pero siempre salió la 0.

**Causa:** `template_variant` escribe `last_template_*` **en el dict que recibe** (`prompts.py:335`), pero:

- `_update_context` recarga un contexto nuevo desde la BD (`pipeline.py:839`) y guarda ese → la mutación se pierde.
- `_render_greeting` carga otro contexto y lo muta sin guardar (`pipeline.py:213-215`).
- `_fast_template` usa `TEMPLATES[...]` sin variantes (`pipeline.py:358`), así que `sale_confirm`, `sale_ask_data` y `sale_declined` son fijos por diseño.

**Evidencia:** `conversations.context_json` no contiene ninguna clave `last_template_*` tras 10 turnos.

**Arreglo:** devolver el contexto mutado desde el render y persistirlo en `_update_context`; añadir variantes a las plantillas de venta.

---

### F6 — ALTO: la dirección se corrompe y contamina el perfil del cliente

```
conversation_facts.delivery_address = "pero quiero 3 no 1"   (source m15)
customers.preferences_json = {"usual_address": "pero quiero 3 no 1", ...}
```

La dirección real («Calle Prueba numero 110, entre Calle A e Calle B») se capturó bien en m11 y se **sobrescribió** en m15.

**Causa:** `_extract_address` (`memory.py:278-280`) acepta el fallback `_ADDRESS_FLEXIBLE`, y `_looks_like_address` (`memory.py:263-267`) solo exige **un dígito** o una palabra de calle → cualquier frase con número pasa.

**Arreglo:**

- Exigir patrón de dirección (vía/calle + número) y rechazar verbos/cantidades («quiero», «no», «pero», «cuánto», «precio»).
- No sobrescribir una dirección ya capturada con un match de menor confianza.
- No escribir `usual_address` del cliente desde un match flexible.

---

### F7 — MEDIO: el saludo se responde con la plantilla de producto

m3 «buenas noches» → NLU `otro` (confianza 0.0) → plantilla de producto.

**Arreglo:** ruta de saludo determinista antes del NLU (léxico: hola/buenas/buen día), responder saludo + oferta breve.

---

### F8 — MEDIO: métricas engañosas en el dashboard

- «Latencia media» solo promedia `llm.chat`: el resto de trazas tiene `duration_ms = null`.
- `pipeline.handle_turn` se registra **2 veces por turno** (entrada con `output: null` + salida), así que «Trazas hoy» cuenta el doble.

**Arreglo:** un único trace por turno con `duration_ms`, o renombrar el de entrada a `pipeline.handle_turn.start`.

---

### F9 — MEDIO: `cuadra.log` es inservible

El 90 % del archivo es ruido de APScheduler: `Running job "tick"` / `executed successfully` cada 5 s. Los mensajes útiles (inbound del cliente) quedan enterrados.

**Arreglo:** `logging.getLogger("apscheduler").setLevel(logging.WARNING)`.

---

### F10 — MEDIO: el compositor LLM nunca se usa

`compose_ms = 0` en 10/10 y `generator = "template"` en 10/10. Toda respuesta sale de plantillas, incluida la pregunta libre de m19. Es la causa de fondo de la sensación robótica.

**Arreglo:** usar `compose` para preguntas abiertas (`consulta_dato`, `otro`) con guardrails, y dejar plantillas para los pasos transaccionales.

---

### F11 — BAJO: nombre del destinatario = usuario de Telegram

La confirmación dice «para Proveedor TresEPD» (username). Conviene pedir/confirmar el nombre de quien recibe.

---

## 4. Lo que sí funcionó

- El contexto **se guarda** correctamente entre turnos (`context_json` con `stage` y `product_id`).
- Captura de zona y teléfono correcta y en un solo turno.
- Sin excepciones, sin duplicados de respuesta, sin bloqueos de cola.
- Latencia p50 3,1 s (dentro del presupuesto).
- Trazas y `traces.log` operativos: el diagnóstico de esta prueba salió de ellos.
- Guardrails sin violaciones.

---

## 5. P1 revisado (orden propuesto)

| # | Cambio | Ataca | Impacto | Estado |
|---|--------|-------|---------|--------|
| 1 | Normalizar `producto` del NLU (`"null"` → `None`) | F1 | Crítico, 5 líneas | Hecho (bloque A) |
| 2 | No cortar la venta si hay producto en contexto / venta abierta | F1 | Crítico | Hecho (bloque A) |
| 3 | No resetear `stage` a DISCOVERY si hay venta abierta | F1 | Crítico | Hecho (bloque A) |
| 4 | Persistir `qty` y usarla en confirmación y creación | F2 | Crítico | Hecho (bloque A) |
| 5 | Resolver por categoría y listar catálogo | F3, F3b | Crítico | Hecho (bloque B) |
| 6 | Cotizar antes de pedir datos de entrega | F3b | Alto | Hecho (bloque B) |
| 7 | Persistir `last_template_*` y variantes en plantillas de venta | F5 | Alto | Hecho (bloque B) |
| 8 | Endurecer `_extract_address` y no sobrescribir dirección válida | F6 | Alto | Hecho (bloque A) |
| 9 | `consulta_dato` con producto: alternativas o respuesta directa | F4 | Alto | Hecho (bloque B) |
| 10 | Ruta de saludo determinista | F7 | Medio | Hecho (bloque B) |
| 11 | Un trace por turno con `duration_ms` | F8 | Medio | Hecho (bloque C) |
| 12 | Silenciar APScheduler en `cuadra.log` | F9 | Medio | Hecho (bloque C) |
| 13 | Usar `compose` para preguntas abiertas | F10 | Medio | Hecho (bloque C) |

**Criterio de aceptación para el re-test:** repetir este mismo guion y obtener **1 venta con cantidad 3**, sin ninguna respuesta repetida literal y sin pedir dos veces el mismo dato.

**Estado al cerrar el bloque B (2026-11-16):** replay del guion con los intents
reales del NLU sobre una BD temporal → 1 venta (`quantity=3`, `amount=1050`,
zona Playa, dirección correcta), **10 respuestas distintas de 10**, ningún dato
pedido dos veces y ninguna venta perdida. Suite: 306 tests OK.

**Estado al cerrar el bloque C (2026-11-16):**

- **F8:** `handle_turn` produce **un solo trace** `pipeline.handle_turn` por turno,
  con `duration_ms` (y otro `ok=0` si el turno revienta). La «latencia media» del
  dashboard ahora promedia el turno completo; el LLM se mide aparte (`avg_llm_ms`).
- **F9:** `apscheduler`, `httpx` y `telethon.network` en `WARNING`. Tras reiniciar,
  `cuadra.log` ya no repite el tick de 5 s; los mensajes de negocio se leen.
- **F10:** el compositor se usa en preguntas abiertas (`consulta_dato` sin dato del
  proveedor, queja, comparación, tema libre) con guardrails y **fallback
  determinista** si el modelo falla. Sonda en vivo: «¿De qué color es y cuánta RAM
  trae?» → `generator=llm`, `compose_ms=2816`, respuesta con `negro` y `8 GB` del
  catálogo. Los pasos transaccionales siguen en plantillas (8 ms el mismo turno).
- **Suite:** 310 tests OK. Replay del guion #2: 10/10 respuestas distintas, 1 venta
  `quantity=3`, `amount=1050`, zona Playa, dirección correcta.
