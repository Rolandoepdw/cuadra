# Análisis del test real — 2026-11-13

**Fecha del test:** 2026-11-13, 20:51–21:46 local (01:51–02:46 UTC)
**Canal:** Telegram userbot (`@cuadra_demo_userbot`, id `1000000002`)
**Configuración:** `sales.mode = autonomous`, `sales.followup.mode = shadow`
**Base de datos:** `backend/data/cuadra.db` (sembrada de cero antes del test)
**Estado:** análisis cerrado. Sin cambios de código aplicados todavía.
**Plan derivado:** [`2026-11-14_PLAN_IMPLEMENTACION.md`](2026-11-14_PLAN_IMPLEMENTACION.md)

---

## 1. Resumen ejecutivo

La infraestructura **no falló**. No se perdió ningún job, no hubo un solo error de LLM y no hubo un solo fallo de envío. Lo que falló fue la capa determinista (extractores, regex, máquina de estados), las plantillas y —muy importante— **el modelo de concurrencia y de tiempos**, que impone un techo de rendimiento muy bajo.

| Métrica | Valor |
|---|---|
| Conversaciones / mensajes | 9 / 185 (90 cliente, 86 bot, 9 TCP) |
| Turns | 83, todos `DONE` |
| Ventas | 5 (2 `CONFIRMED`, 3 atascadas en `DISPATCHED`) |
| Escalaciones | 1 (tardó 9,5 min y respondió mal) |
| Jobs | 260, todos `COMPLETED`, 0 fallidos |
| Llamadas LLM | 115, 115 OK, 0 errores |
| Latencia de respuesta | p50 5,3 s · p95 9,9 s · max 19 s |
| Inbound→outbound observado | mediana 8–11,5 s · max 30 s |

Ninguna queja de los clientes se explica por caída de servicio. Se explica por 11 bugs concretos y por un diseño de cola que serializa todo el sistema.

**Conclusión clave:** arreglar solo los prompts no cambia nada. El 80 % de la torpeza viene del motor determinista, y la lentitud viene de la cola de un solo worker, del slot único del modelo y del gap global de 8 s.

---

## 2. Método y evidencia

- Consultas SQL directas sobre `cuadra.db` (solo lectura): conversaciones, mensajes, turns, ventas, jobs, eventos, escalaciones, `llm_calls`, `conversation_facts`, `sender_state`.
- `grep` selectivo sobre `cuadra.log` y `llm.log` para descartar fallos de infraestructura.
- Lectura del código de las rutas implicadas.
- No se revisaron logs completos: solo patrones de error agregados.

---

## 3. Qué NO falló (para acotar el problema)

- **Jobs:** 260/260 `COMPLETED`. 0 `FAILED`, 0 reintentos agotados.
- **LLM:** 115/115 llamadas OK. Sin timeouts, sin JSON inválido, sin caídas del servidor.
- **Envíos:** 0 fallos. Los peers de los 4 TCP eran correctos (`1000000003`, `1000000004`, `1000000001`, `1000000005`).
- **Turns:** 83/83 `DONE`. No hubo turnos colgados.
- **Ruido irrelevante:** 6 `Bad Gateway` de aiogram (Bot API) y 15 ticks del scheduler perdidos. El tráfico del test fue 100 % userbot, así que el Bot API no participó.

Es decir: **el sistema no se rompió; hizo exactamente lo que el código dice, y el código dice cosas equivocadas.**

---

## 4. Bugs de código

### P0 — rompen el flujo de negocio

#### B1. La zona no se captura si el cliente no usa la palabra clave

`backend/cuadra/domain/catalog.py:338-362`

`resolve_zone` solo busca el nombre del municipio dentro de una frase si `_ZONE_HINT_RE` encuentra antes alguna de:
`vivo|vive|resido|estoy en|entrega|entregar|envío|zona|municipio|dirección|domicilio|reparto|llevar|mandar`.

Si no, devuelve `None`.

Evidencia (c4, Pizza Personal / Proveedor Dos):

```
m66 CLI 02:01:03  Calle 51 #4410 55545678
m68 BOT 02:01:18  ...me falta la zona de entrega...
m74 CLI 02:02:01  Esto es Marianao        ← rechazado: no hay hint
m76 BOT 02:02:22  ...me falta la zona de entrega...
```

La conversación murió en `CAPTURE_DATA`, nunca se creó la venta y **Proveedor Dos no recibió nada**.
Mismo patrón en c5 con `"soy de Mariano"` (sin hint válido y con errata).

#### B2. Bucle post-venta: cualquier intención de compra devuelve la venta vieja

`backend/cuadra/ai/pipeline.py:404-421`

```python
existing = sales_domain.active_sale_for_conversation(conn, conversation["id"])
if existing is not None:
    guard = _sale_guard(product, float(existing["amount"]))
    reply, generator = _fast_template(..., "sale_created", order_code=existing["order_code"])
    return {... "stage": "DONE", "sale": existing}
```

No compara producto. c5, c6 y c8 recibieron "Tu pedido V-00000X quedó registrado" repetidamente, incluso pidiendo otro producto. Bloquea cross-sell y hace que el bot parezca sordo.

#### B3. Los acks de los TCP se pierden y se aplican a la venta equivocada

`backend/cuadra/domain/sales.py:649+`

Dos defectos encadenados:

1. `_ACK_CONFIRM = \b(RECIBID[OA]|CONFIRMAD[OA]|PREPARAD[OA]|LISTO)\b` → **`"RECIVIDO V-000003"` no hace match**, así que ni siquiera se llega a leer el código de orden.
2. `_ack_sale` sin código elige `ORDER BY id LIMIT 1` entre `DISPATCHED` **y** `CONFIRMED`: la venta más antigua, no la que toca.

Resultado: el `RECIBIDO` de Proveedor Tres confirmó la V-000001 (ya confirmada) y las ventas 3, 4 y 5 quedaron en `DISPATCHED` para siempre.
El triage marcó `ambiguous=true` y **se descartó en silencio**.

#### B4. La pregunta de escalación es el nombre crudo del slot

`backend/cuadra/ai/pipeline.py:616-640`

```python
if intent.intencion == "consulta_dato":
    missing = ", ".join(intent.datos_faltantes) if intent.datos_faltantes else text
    ...
    return reply, True, generator, missing   # → escalation_question
```

El intent de c3 fue `{"intencion": "consulta_dato", "datos_faltantes": ["municipio"]}` ante el mensaje *"pero si no sabes cuál es mi municipio"* (un reto, no una pregunta de dato). Al TCP le llegó literalmente:

> Producto: PC de Escritorio Ryzen 5
> **"municipio"**

Proveedor Tres contestó "Es Marianao" y el bot se lo devolvió al cliente como **su** zona, cuando la real era Diez de Octubre.

Timeline: escalación creada 02:01:01 → enviada 02:04:33 → respondida 02:10:26 → cliente notificado 02:10:33. **9,5 minutos para empeorar el dato.**

### P1 — degradan la conversación

#### B5. Fuga de placeholder al cliente

`backend/cuadra/ai/guardrails.py:176-192` + `backend/cuadra/ai/context.py:144`

`redact_stale_prices` sustituye precios viejos por `"[precio anterior]"` en el historial que ve el modelo, y el modelo lo copió tal cual:

> "El Ventilador de Torre está en **[precio anterior]** y lo entregamos en Centro Habana" (m129, m131)

Nadie valida la salida contra placeholders internos.

#### B6. Zona del producto presentada como zona del cliente

`backend/cuadra/ai/pipeline.py` (`_DEFERRED_ZONE`, `_reply_zone`)

El bot dijo "lo entregamos en Centro Habana" (primera zona del producto) a una clienta de Marianao. Falta una regla dura: nunca afirmar una zona que no sea la confirmada del cliente.

#### B7. Dirección truncada

`backend/cuadra/ai/memory.py:259-267`

`"Calle 10 #123"` → `"Calle 10"`. La regex corta en `#`; se pierde el número, que es el dato que el repartidor necesita.

#### B8. Destinatario no se actualiza

`backend/cuadra/ai/memory.py:192-202`

"para Luis Ramirez" no cambió `recipient_name`; la venta quedó a nombre de "Carlo Enrrique".

#### B9. "No" en `CAPTURE_DATA` no se maneja

c4 m53/m55: el cliente dijo "No" y el bot repitió la misma pregunta de slots, idéntica, sin ofrecer alternativa ni preguntar qué falta.

#### B10. Mensajes de TCP sin caso abierto se ignoran

`backend/cuadra/ai/handlers.py:1180-1198`

Proveedor Cuatro escribió "j" y "/start": `OTHER` + `ambiguous=true` → evento, nada más. **El TCP nunca recibe respuesta.** Un proveedor que escribe y no obtiene nada concluye que el sistema está roto.

#### B11. Prioridad invertida para TCP

`backend/cuadra/ai/queue.py` + `backend/cuadra/config.py:167-168`

`PRIORITY_CUSTOMER = 10`, `PRIORITY_TCP = 50`. La escalación esperó **3,5 min** de cola detrás de mensajes de clientes.

### P2 — ruido operativo

- 15 ticks del scheduler perdidos (hasta 4,8 s). Causa probable: trabajo síncrono pesado dentro del event loop (embeddings de `retrieval.embed_texts`, SQLite) mientras corre el único worker.
- `OUT_OF_HOURS_DELAY` (5–15 s) se activó al cruzar las 21:00 y empeoró la segunda mitad del test.

---

## 5. Comportamiento del agente

El modelo **no falló ninguna llamada** y la redacción es correcta. Lo que se percibió como "tonto" es esto:

| Síntoma | Causa real |
|---|---|
| Respuestas idénticas 6 veces seguidas (c3 m17/m19/m22/m31/m34/m35) | Plantilla `ask_which_product` elegida por el motor ante `otro`/baja confianza. El modelo no participó. |
| "¿te interesa o no?" con presión | Plantilla fija, sin memoria de que ya se preguntó. |
| Cero empatía ante "eres tonto", "responde con empatía" | La ruta de `queja` no se activó; cayó en `otro`. |
| Aceptó "Es Marianao" como dato del cliente | B4: el motor le inyectó la respuesta del TCP como contexto veraz. |
| Repitió "tu pedido quedó registrado" | B2: el motor devolvió la plantilla, el modelo no redactó. |

**El modelo no es el problema principal.** Es un sintetizador de frases correcto al que se le da estado incorrecto.

---

## 6. Por qué Proveedor Dos y Proveedor Cuatro no recibieron nada

No hubo fallo de envío: peers correctos, 0 jobs fallidos.

- **Proveedor Dos**: su única conversación (c4, Pizza) murió en `CAPTURE_DATA` por B1. Nunca existió venta que despachar.
- **Proveedor Cuatro**: ningún cliente preguntó por sus productos (0 menciones en 185 mensajes) y sus dos mensajes entrantes se ignoraron por B10.

Uno es un bug; el otro es un problema de guion del test.

---

## 7. Modelo de tiempos y concurrencia (por qué se sintió lentísimo)

Hay **tres esperas distintas** que no deben confundirse.

### 7.1 Debounce del turno (no es una espera fija)

`backend/cuadra/config.py:145-148`

```python
TURN_DEBOUNCE_S = 2.5   # silencio requerido
TURN_MAX_WAIT_S = 8     # techo duro desde el primer mensaje del turno
```

Funcionamiento real:

- Cada mensaje entrante **re-arma** el temporizador a `ahora + 2.5 s`.
- El turno se procesa cuando pasan 2,5 s sin mensajes nuevos.
- El tope de 8 s evita que un cliente que escribe sin parar bloquee la respuesta para siempre.

Ejemplo:

```
t=0.0  "Hola"
t=1.5  "quiero la pizza"
t=1.5  → due_at = 4.0   → responde a los 4.0 s
```

```
t=0.0 "Hola"
t=2.0 "la pizza"
t=4.0 "personal"
t=6.0 "de 5"
t=8.0 → cap: responde aunque siga escribiendo
```

No es "espero 8 s y ejecuto". Es **"espero 2,5 s de silencio, con techo de 8 s"**.

### 7.2 Delay humano por envío

`backend/cuadra/ai/handlers.py:480` (`_human_delay`), usado antes de cada `send_text`.

- En horario: 1–4 s.
- Fuera de horario (después de las 21:00): 5–15 s.

Es cosmético (simula que alguien escribe) y **bloquea al worker**, porque el worker es único.

### 7.3 Gap global del userbot (el más importante)

`backend/cuadra/config.py:72-77`

```python
USERBOT_MIN_SEND_GAP_S = 8
```

- **No es solo para difusiones.** Aplica a **todo** envío del userbot: respuestas a clientes, mensajes a TCP y difusiones.
- Es **por cuenta**, no por chat: después de cualquier envío, el siguiente envío espera ~8 s.
- Implementación: `rate_limit.mark_action("userbot")` al enviar; `_respect_userbot_gap` consulta `local_gap_remaining` y, si falta tiempo, lanza `RateLimited(penalize=False)` con `retry_at = ahora + restante + 0,25 s`.
- El job **no duerme**: vuelve a `PENDING` con `next_retry_at` y no consume intentos ni cuenta como fallo.
- El bucle de la cola, mientras el gap está activo, deja las difusiones en cola (`allow_broadcast=False`) y sigue atendiendo clientes/TCP.

Consecuencia aritmética: **techo global de ~7,5 mensajes salientes por minuto**, para todas las conversaciones juntas.

En el test había 5–6 conversaciones activas a la vez. Si cinco clientes escriben en el mismo intervalo, el quinto espera ~32 s solo por el gap. **El máximo observado fue 30 s.** El gap explica la cola del p95.

### 7.4 La cola tiene UN solo worker

`backend/cuadra/ai/queue.py:370-394`

```python
async def _loop():
    while not _stop.is_set():
        ...
        job = _claim(conn, allow_broadcast=wait <= 0)
        ...
        await _process(job)   # ← secuencial, espera a que termine
```

`main.py` llama a `queue.start()` una sola vez → **un único worker para todo el sistema**: clientes, TCP, difusiones y mantenimiento.

Consecuencia: un `process_turn` ocupa el worker durante `intent (5 s) + compose (3,7 s) + delay (1–4 s) + gap (hasta 8 s)` ≈ **13–17 s**. Todo lo demás espera.

### 7.5 El modelo también es serial

`backend/cuadra/ai/llm_server.py:195-216`

```
-np 1     → un solo slot
-c 8192   → 8192 tokens para ese slot
```

llama.cpp atiende **una petición a la vez**. Aunque se añadieran más workers, las llamadas harían cola dentro del servidor.

El cliente (`llm.py`) crea un `httpx.AsyncClient` por llamada y **no tiene semáforo**: el límite real está en el servidor.

### 7.6 Capacidad efectiva

| Recurso | Límite actual | Efecto |
|---|---|---|
| Worker de cola | 1 job a la vez | todo serializado |
| Slots del LLM | 1 (`-np 1`) | una inferencia a la vez |
| Gap de envío | 8 s por cuenta | ~7,5 msg/min globales |
| Delay humano | 1–4 s (5–15 fuera de horario) | +1–15 s por envío |

Con 9 conversaciones simultáneas el sistema está **por encima de su capacidad**. El test lo demostró.

---

## 8. Plan propuesto

### P0 — sin esto el producto no es usable (1–2 días)

| # | Cambio | Archivo | Test |
|---|---|---|---|
| 1 | Resolver zona por nombre canónico/alias dentro de la frase **sin exigir hint**; hint solo para desambiguar | `domain/catalog.py` | "Esto es Marianao", "soy de Mariano", "Miramar, Playa", "zona: Diez de Octubre" |
| 2 | `_maybe_handle_sale`: cortocircuito **solo si el producto coincide**; si no, permitir segunda venta | `ai/pipeline.py` | comprar A y luego B → 2 ventas |
| 3 | Ack TCP: aceptar `RECIVIDO/RECIBIDO/RECIVÍ/RECIBI`; si hay `V-######`, honrarlo siempre; sin código, elegir **solo** `DISPATCHED`; `ambiguous` → avisar, nunca silencio | `domain/sales.py`, `ai/tcp_triage.py` | errata + código; dos ventas abiertas |
| 4 | Escalación: pregunta real con contexto (producto + duda textual + qué falta). **Nunca** escalar datos del propio cliente (zona/dirección/teléfono) | `ai/pipeline.py:616-640`, `prompts.py` | "no sabes mi municipio" → no escala |
| 5 | Validación de salida: rechazar `[precio anterior]`, `[DIR]`, `[TEL]`, `[NOMBRE]` y cualquier `[...]` interno | `ai/guardrails.py` | texto con placeholder → bloqueado |
| 6 | Regla dura de zona: solo se afirma la zona confirmada del cliente | `prompts.py` + guardrail | cliente Marianao no ve "Centro Habana" |
| 7 | **Concurrencia y tiempos** (ver §7): bajar el gap 1:1, separar workers, decidir `-np` | `queue.py`, `config.py`, `llm_server.py` | medir p50/p95 bajo carga sintética |

### P1 — calidad y confianza (2–3 días)

| # | Cambio |
|---|---|
| 8 | `_extract_address`: capturar `#123`, `No. 123`, `e/ 7 y 9` |
| 9 | `_extract_name`: "para X", "a nombre de X", "soy X" |
| 10 | `CAPTURE_DATA`: manejar negación → ofrecer alternativa, sin repetir la misma frase |
| 11 | TCP sin caso abierto: responder siempre ("no tengo casos abiertos ahora") y registrar |
| 12 | `PRIORITY_TCP` ≤ `PRIORITY_CUSTOMER` o lane dedicado |
| 13 | Anti-repetición: si la plantilla es idéntica a la anterior, variar o escalar a composición libre |

### P2 — rendimiento (después)

| # | Cambio |
|---|---|
| 14 | Pre-filtro determinista antes del `intent`: "Ok", "No", "j", emojis, `/start` → sin LLM. Ahorra ~40 % de llamadas y 5 s en turnos triviales |
| 15 | Timeout/fallback del `intent`: si tarda >4 s, seguir con heurística |
| 16 | Revisar `OUT_OF_HOURS_DELAY`: 5–15 s es demasiado para confirmar un pedido |
| 17 | Mover embeddings y trabajo CPU al thread pool (`asyncio.to_thread`) para no bloquear el event loop |

### Protocolo de re-test

- 4 TCPs, cada uno con al menos **un cliente guionizado** que pida un producto suyo.
- Guion fijo de 8 escenarios: saludo → precio → stock → zona difícil ("esto es Marianao") → datos completos → confirmación → ack con errata → segundo producto.
- Medir: p50/p95 inbound→outbound, escalaciones (n, tiempo, calidad de pregunta), ventas por TCP, acks aplicados correctamente.
- Criterio de éxito: 4/4 TCP con ≥1 venta, 0 ventas atascadas en `DISPATCHED` >5 min, p95 < 12 s, 0 placeholders.

---

## 9. Decisiones abiertas

1. **Segunda venta (B2):** ¿varias ventas activas por conversación o una sola?
2. **Alcance de la escalación (B4):** ¿qué datos son legítimos para el TCP?
3. **Acks ambiguos (B3):** ¿bloquear y avisar, o aplicar al `DISPATCHED` más reciente?
4. **Pre-filtro (P2-14):** ¿entra en P0?
5. **Re-test:** ¿guion automatizado primero o clientes reales otra vez?
6. **Gap del userbot:** ¿bajarlo de 8 s a 2–3 s para 1:1 y dejarlo alto solo para difusiones?
7. **Concurrencia del LLM:** ¿subir `-np` (y bajar `-c` por slot) o mantener `-np 1` con semáforo y varios workers?
