# Plan de implementación y política de tiempos

**Origen:** análisis del test real del 2026-11-13 (`2026-11-13_ANALISIS_TEST_REAL.md`).
**Decisión del operador (2026-11-14):**
- Se aceptan todas las propuestas del análisis.
- Prioridad 1: **no ser baneados**. Prioridad 2: **rapidez**.
- Queda descartado el realismo / delay para simular humanidad.
- El agente se comporta rígido y repite mensajes: hay que corregirlo.

---

## 1. Diagnóstico del agente: por qué se siente "tonto" y rígido

No es el modelo. Es cómo se le habla y cómo se le sustituye. Cinco causas, en orden de impacto:

### 1.1 El system prompt contiene frases literales que el modelo copia

`backend/cuadra/ai/prompts.py:31`

```
- Si el cliente insiste en saber si eres humano, responde con naturalidad y desvía
  al asunto comercial: "Aquí manejamos todo por mensaje, así es más rápido ¿te interesa o no?"
```

Esa frase exacta aparece **5 veces** en c3 (m17, m19, m31, m34, m35), incluso cuando el cliente no preguntó si era humano. El modelo toma el ejemplo como plantilla.

**Regla:** en un prompt de estilo, nunca poner una frase completa entre comillas. Describir el tono, no dar el texto. Si hace falta un ejemplo, marcarlo como "ejemplo de tono, NO lo copies".

### 1.2 Plantillas fijas, sin variación

`prompts.TEMPLATES["ask_which_product"]` es una sola cadena. c4 la recibió 4 veces idéntica (m24, m26, m27 y de nuevo tras "No").

**Regla:** 3–4 variantes por plantilla, elegidas de forma determinista por `(conversation_id + turn_id)`, y prohibido repetir la usada en el turno anterior.

### 1.3 El modelo no sabe lo que acaba de decir

El contexto incluye historial, pero no hay una instrucción explícita de no repetir. Con temperatura 0,4 y el mismo contexto, converge a la misma frase.

**Regla:** inyectar los últimos 2 mensajes del bot con la instrucción "no repitas literalmente lo que ya dijiste; si ya preguntaste esto, cambia el ángulo".

### 1.4 El bucle `otro` no tiene salida

Si el NLU devuelve `otro` y no hay producto, se compone texto genérico. El cliente insiste y recibe lo mismo. No hay contador de intentos fallidos ni ruta de rescate.

**Regla:** 2 `otro` consecutivos → pregunta abierta distinta ("¿qué estás buscando exactamente?"). 3 → derivar a operador humano.

### 1.5 `queja` no se activa

"eres tonto", "responde", "feo" → `otro`. El bot nunca se disculpa ni cambia de tono.

**Regla:** léxico determinista de queja/insulto (`tonto|estúpido|no sirves|no entiendes|responde|feo|basura|inútil`) → ruta `queja` con plantilla de desescalada + composición con instrucción de empatía.

### 1.6 El NLU está sobrecargado y sesgado

- Prompt medio: **2171 tokens** para una salida de 37 tokens. Latencia media 5,06 s.
- Se le manda el system prompt completo de ventas + 12 mensajes de historial para clasificar una intención.
- No distingue una pregunta real de un reto retórico ("pero si no sabes cuál es mi municipio" → `consulta_dato` + `datos_faltantes: ["municipio"]`).

**Regla:** el NLU usa un prompt mínimo (tarea + último mensaje + 2 mensajes de contexto), no la persona completa. Y `datos_faltantes` pasa a ser estructurado (ver §3.4).

### 1.7 La máquina de estados es rígida

- Un saludo reinicia el flujo aunque haya una venta en curso (H-12 lo hace a propósito, pero se pasa de freno).
- `ask_which_product` se dispara también con confianza 0,2.
- La venta activa secuestra cualquier compra nueva (B2).

---

## 2. Política de tiempos

### 2.1 Principio

Se elimina todo lo cosmético. Solo quedan tres esperas legítimas:

1. **Debounce del turno**: para no responder a medias mientras el cliente sigue escribiendo.
2. **Pacing de la cuenta**: para no ser baneados.
3. **Backoff**: solo cuando algo falla.

Todo lo demás (simular escritura, lentitud nocturna, "margen humano") se va a cero.

### 2.2 Tabla completa de esperas

| # | Espera | Dónde | Para qué | Actual | Recomendado | Mínimo seguro |
|---|---|---|---|---|---|---|
| 1 | `TURN_DEBOUNCE_S` | `config.py:147` | silencio antes de procesar el turno | **2,5 s** | **0,8 s** | **0,4 s** |
| 2 | `TURN_MAX_WAIT_S` | `config.py:148` | techo del turno si el cliente no para | **8 s** | **2,5 s** | **1,5 s** |
| 3 | `POLL_INTERVAL_S` | `queue.py:27` | espera del worker cuando no hay jobs | **1,0 s** | **0,2 s** | **0,05 s** |
| 4 | `TYPING_DELAY_MIN/MAX` | `config.py:176-177` | simular escritura | **1–4 s** | **0 / 0** | **0** |
| 5 | `typing()` (llamada API) | `telegram_user.py:533` | indicador "escribiendo" | siempre | **off** | off |
| 6 | `OUT_OF_HOURS_DELAY_MIN/MAX` | `config.py:183-184` | lentitud nocturna | **5–15 s** | **0 / 0** | **0** |
| 7 | `USERBOT_MIN_SEND_GAP_S` | `config.py:74` | gap global entre envíos | **8 s** | **1,5 s** (o token bucket) | **1,0 s** |
| 8 | margen del gap | `handlers.py:~70` | evitar el borde exacto | **0,25 s** | **0,05 s** | **0** |
| 9 | `BROADCAST_TICK_S` | `config.py:173` | 1 post por tick | **5 s** | **5 s** | 5 s |
| 10 | `broadcast.min_interval_s` | settings / `006` | intervalo por grupo | **300 s** | **600 s** | **300 s** |
| 11 | `broadcast.max_interval_s` | settings / `006` | techo del jitter por grupo | **1200 s** | **1800 s** | 1200 s |
| 12 | `max_posts_per_day` (grupo) | `006` | tope diario por grupo | **20** | **12** | **10** |
| 13 | `QUEUE_BACKOFF_BASE_S` | `config.py:156` | reintento tras fallo | **5 s** | **1 s** | 1 s |
| 14 | `QUEUE_BACKOFF_MAX_S` | `config.py:157` | techo del backoff | **300 s** | **60 s** | 60 s |
| 15 | `QUEUE_RATE_LIMIT_JITTER_S` | `config.py:164` | jitter tras FloodWait | **30 s** | **5 s** | 0 |
| 16 | `COMPOSE_RETRY_DELAY_S` | `llm.py:30` | reintento de transporte | **1,0 s** | **0,3 s** | 0,3 s |
| 17 | `LLM_TIMEOUT_S` | `config.py:89` | techo de una llamada | **120 s** | **30 s** | **15 s** |
| 18 | `TELEGRAM_PEER_FLOOD_COOLDOWN_S` | `config.py:75` | castigo real de Telegram | **3600 s** | **3600 s** | **1800 s** |
| 19 | `ESCALATION_TIMEOUT_MIN` | `config.py:187` | timeout de respuesta del TCP | **30 min** | **15 min** | 10 min |
| 20 | per-chat floor | *nuevo* | mínimo entre 2 mensajes al mismo chat | no existe | **1,0 s** | **1,0 s** |

Notas:

- **#4 y #6 se eliminan por decisión del operador.** Ahorran 1–4 s por envío en horario y 5–15 s fuera de horario. Es el mayor ahorro individual.
- **#5**: cada `typing()` es una petición extra a Telegram. Con 9 conversaciones eso duplica el volumen de requests. Se apaga por defecto y se deja como opción de UI.
- **#7**: es el techo real del sistema. 8 s → 1,5 s multiplica por ~5 el throughput de envíos.
- **#10–#12 no se tocan a la baja**: publicar en grupos es la superficie con más riesgo de ban. Ahí la prioridad 1 manda.
- **#18 no se reduce** por debajo de 1800 s: es la respuesta a un castigo real.

### 2.3 Por qué 1,0 s es el mínimo

No hay cifra oficial de Telegram. Estos son valores de ingeniería conservadores, basados en el comportamiento observado de la API:

- Responder a un usuario **que escribió primero** es de bajo riesgo. El riesgo alto es el mensaje frío a desconocidos (PeerFlood).
- Una cuenta estable tolera ~1 mensaje/s a chats distintos de forma sostenida.
- Por debajo de 1 s entre mensajes al **mismo** chat aparecen FloodWait cortos.
- En ráfagas de muchos chats distintos, el límite práctico ronda 20–30 mensajes/minuto.

Por eso el mínimo global es **1,0 s**, y además se añade un **token bucket**:

```
capacidad: 4 mensajes de ráfaga
recarga:   1 token cada 1,5 s  (≈ 40/min de techo teórico)
tope duro: 20 mensajes/minuto y 300/hora
```

El token bucket da rapidez en el caso normal (2–4 conversaciones activas: salen sin esperar) y frena solo cuando hay saturación.

### 2.4 Reglas duras anti-ban (no negociables)

1. **Nunca mensaje frío.** Solo se responde a quien escribió primero. Las difusiones van solo a grupos donde la cuenta ya está.
2. **Nunca dos mensajes al mismo chat con menos de 1 s.**
3. **Nunca más de 20 mensajes/minuto** ni 300/hora desde la cuenta.
4. **Difusiones a grupos**: mínimo 5 min por grupo (recomendado 10), máximo 12/día por grupo.
5. **Cuenta nueva (<30 días) o con poca actividad**: duplicar todos los intervalos. Añadir perfil `conservador` en configuración.
6. **Ante FloodWait**: parar la cuenta entera, no solo el job. Ya está implementado; se mantiene.
7. **Ante PeerFlood**: 1 hora de pausa y bajar el ritmo un 50 % durante 24 h (gobernador adaptativo, ver §4).

### 2.5 Presupuesto de latencia resultante

| Etapa | Hoy | Después |
|---|---|---|
| Debounce | 2,5 s | 0,8 s |
| Espera en cola | 0–30 s | 0–0,5 s |
| `intent` (LLM) | 5,0 s | 1,8–2,2 s |
| `compose` (LLM) | 3,7 s | 2,0–2,5 s |
| Delay humano | 1–15 s | 0 |
| Gap de envío | 0–8 s | 0–1,5 s |
| **Total p50** | **~9 s** | **~3 s** |
| **Total p95** | **~10 s** | **~6 s** |

El `intent` baja por el recorte del prompt (2171 → ~600 tokens), no por magia.

---

## 3. Plan de implementación

### Fase 0 — Instrumentación (0,5 día)

- Añadir desglose de latencia por turno en `events` (`debounce_ms`, `queue_ms`, `intent_ms`, `compose_ms`, `send_wait_ms`).
- Script `scripts/lab/load_test.py`: 4 clientes simulados, guion fijo, mide p50/p95 y ventas.
- Sin esto, cualquier cambio es a ciegas.

### Fase 1 — Bugs P0 de negocio (2–3 días)

**1.1 Zona sin hint** — `domain/catalog.py:338`
- Buscar nombre canónico y alias en la frase completa con límite de palabra, sin exigir `_ZONE_HINT_RE`.
- El hint pasa a ser solo desambiguador cuando aparecen 2+ municipios.
- Tests: "Esto es Marianao", "soy de Mariano", "Miramar, Playa", "zona: Diez de Octubre", "Calle 51 #4410 55545678".

**1.2 Acks TCP** — `domain/sales.py`, `ai/tcp_triage.py`
- `_ACK_CONFIRM` acepta `RECIBIDO|RECIVIDO|RECIBI|RECIVÍ|CONFIRMADO|CONFIRMO|LISTO|PREPARADO`.
- Si hay `V-######` y pertenece al proveedor: se aplica siempre, aunque la regex falle.
- Sin código: solo se elige entre `DISPATCHED` (nunca `CONFIRMED`).
- Varias candidatas: no se toca estado, se crea revisión y se avisa al operador.
- Siempre se responde al TCP.

**1.3 Segunda venta** — `ai/pipeline.py:404`
- Invariante: una venta abierta por `(conversación, producto)`.
- `_maybe_handle_sale` cortocircuita solo si `existing.product_id == product.id`.
- Si piden otro producto: venta nueva.

**1.4 Escalación con contexto y alcance** — `ai/pipeline.py:616`, `prompts.py`
- Lista blanca: stock, variantes, garantía, tiempo de entrega, precio, estado de pedido.
- Lista negra: zona, dirección, teléfono, nombre. Eso se pregunta al cliente.
- Pregunta real: producto + texto literal del cliente + dato pedido.
- Si el dato pedido está en la lista negra → no se escala, se pregunta al cliente.
- Si el mensaje no es una pregunta → no se escala.

**1.5 Placeholders y zona** — `guardrails.py`, `prompts.py`
- `validate_reply` rechaza cualquier `[...]` interno (`[precio anterior]`, `[DIR]`, `[TEL]`, `[NOMBRE]`).
- Regla dura: la única zona afirmable es `facts.zone_name`.

### Fase 2 — Concurrencia y tiempos (2 días)

**2.1 Workers separados**
- `queue.py`: N workers configurables (`CUADRA_QUEUE_WORKERS=4`).
- Semáforo de 1 alrededor de `llm.chat` mientras `-np 1`.
- Al subir workers: mover el chequeo de gap a **justo antes** del `send_text` y reservar el hueco de forma atómica (`mark_action` antes de enviar). Hoy hay carrera.

**2.2 Token bucket** — `channels/rate_limit.py`
- Sustituir el gap fijo por bucket (capacidad 4, recarga 1/1,5 s) + topes 20/min y 300/h.
- Añadir floor por chat de 1 s.
- `sender_state` gana columnas `tokens`, `refilled_at`.

**2.3 Eliminar delays cosméticos**
- `_human_delay` → 0.
- `typing()` apagado por defecto.
- `OUT_OF_HOURS_DELAY` → 0.

**2.4 Modelo**
- Medir `-np 2` con `-c 8192` (4096/slot). Si la VRAM lo permite y la calidad no cae, adoptar.
- Si no, mantener `-np 1` + semáforo.

**2.5 Event loop**
- `retrieval.embed_texts` y cualquier trabajo CPU → `asyncio.to_thread`.
- Esto elimina los ticks perdidos.

### Fase 3 — Agente (2–3 días)

**3.1 Reescribir el system prompt** — `prompts.py`
- Quitar todas las frases literales copiables.
- Añadir: "No repitas literalmente lo que ya dijiste. Si ya preguntaste algo, cambia el ángulo o avanza."
- Añadir regla de zona (solo la confirmada).
- Añadir regla de no escalar datos del cliente.

**3.2 Variantes de plantilla**
- `TEMPLATES` pasa a listas de 3–4 variantes.
- Selección determinista por `(conversation_id + turn_id)`, excluyendo la anterior.

**3.3 Ruta de queja**
- Léxico determinista → `queja` antes de llamar al LLM.
- Plantilla de desescalada + composición con instrucción de empatía.

**3.4 NLU ligero y estructurado**
- Prompt mínimo (tarea + último mensaje + 2 de contexto): ~600 tokens.
- `datos_faltantes` pasa a `[{"dato": str, "origen": "producto|cliente"}]`.
- Distinguir pregunta real de reto retórico.

**3.5 Rescate del bucle `otro`**
- Contador en contexto. 2 → pregunta abierta distinta. 3 → operador.

**3.6 Anti-repetición**
- Inyectar últimos 2 mensajes del bot.
- Si la respuesta es ≥90 % igual a la anterior, recomponer una vez con instrucción de variar.

### Fase 4 — P1/P2 (2–3 días)

- `_extract_address`: `#123`, `No. 123`, `e/ 7 y 9`.
- `_extract_name`: "para X", "a nombre de X", "soy X".
- `CAPTURE_DATA`: negación → alternativa.
- TCP sin caso: responder siempre.
- `PRIORITY_TCP` ≤ `PRIORITY_CUSTOMER`.
- Pre-filtro determinista de triviales (solo sin estado abierto).
- Timeout del `intent` con fallback heurístico a 4 s.

### Fase 5 — Re-test

- Fase A: `load_test.py` con 4 clientes simulados y guion de 8 escenarios.
- Fase B: 4 TCP reales, 15 min, mismo guion.
- Criterios: 4/4 TCP con venta, 0 ventas atascadas >5 min, p95 < 8 s, 0 placeholders, 0 baneos.

---

## 4. Gobernador adaptativo (protección anti-ban)

Nuevo módulo pequeño en `channels/rate_limit.py`:

- Estado por cuenta: `rate_factor` (1,0 normal).
- Al recibir FloodWait: `rate_factor = max(0,5, rate_factor - 0,25)` y cooldown obligatorio.
- Al recibir PeerFlood: `rate_factor = 0,5` durante 24 h.
- Recuperación: +0,05 por hora sin incidentes, techo 1,0.
- Todos los intervalos se multiplican por `1/rate_factor`.

Así el sistema se auto-frena cuando Telegram protesta, sin intervención manual, y vuelve a acelerar solo.

---

## 5. Decisiones registradas

1. Varias ventas abiertas por conversación, una por producto. ✅
2. Escalación solo con datos del proveedor; nunca datos del cliente. ✅
3. Acks: determinista con un candidato, operador con varios, siempre responder al TCP. ✅
4. Pre-filtro de triviales en P0, acotado a turnos sin estado. ✅
5. Re-test con guion automatizado primero, amigos después. ✅
6. Gap 1:1 de 8 s → 1,5 s + token bucket. ✅
7. LLM: mantener `-np 1` + semáforo; probar `-np 2` con medición. ✅
8. Eliminar todo delay cosmético (realismo descartado). ✅
9. Prioridad 1 anti-ban, prioridad 2 rapidez. ✅
