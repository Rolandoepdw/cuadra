---
tipo: guia
proyecto: Cuadra
estado: operativo
actualizado: 2026-10-19
tags: [telegram, token, pruebas, hito4, hito5]
---

# 07 — Puesta en marcha de Telegram (bot + cuenta de TCP de prueba)

> Objetivo: tener (1) un **bot** que atiende a los clientes y (2) una **segunda cuenta de Telegram** que hace de TCP/proveedor, para probar de punta a punta el Hito 4 (conversación con cliente) y preparar el Hito 5 (escalado al TCP).
>
> Regla de oro: **el bot NO es una cuenta normal.** Es un bot creado por @BotFather. Los TCPs y los clientes hablan con el bot desde cuentas normales (las tuyas).

## 0.1 Datos de esta instalación (ya configurados)

| Pieza | Valor | Dónde vive |
|---|---|---|
| Bot | **`@CuadraHabanaBot`** (`t.me/CuadraHabanaBot`) | — |
| Token del bot | `8841263645:AAEc...7ug` | `backend/.env` (`CUADRA_TELEGRAM_TOKEN`) — **nunca en git** |
| Cliente de prueba | `@Rolandoepd` — **ID `1267804486`** | cuenta personal |
| TCP de prueba | `@PeanutEmperor` — **ID `7516390666`** | `suppliers.id=1` ("TCP Prueba (PE)", 3 productos) |

> Pendiente (manual, una vez): entrar con la cuenta del TCP (`@PeanutEmperor`) al chat del bot y pulsar **Start**, para que el bot pueda escribirle en el Hito 5.

---

## 0. Qué necesitas

| Pieza | Cuenta | Para qué | Variable |
|---|---|---|---|
| Bot de Cuadra | (ninguna cuenta) | Atiende a los clientes y, en el Hito 5, escribe a los TCPs | `CUADRA_TELEGRAM_TOKEN` |
| Cliente de prueba | Tu cuenta personal | Simula al comprador | — |
| TCP de prueba | Tu **segunda** cuenta | Simula al proveedor que responde datos faltantes | `suppliers.telegram_id` |

> Telegram permite tener varias cuentas con **distintos números** (o la misma App con "Add Account"). Para el TCP suele bastar un número distinto. Si no tienes, cualquier SIM sirve para recibir el SMS de alta.

---

## 1. Crear el bot (5 minutos, en tu cuenta personal)

1. Abre Telegram y busca **`@BotFather`** (el oficial, tiene el check azul).
2. Envía `/start` y luego `/newbot`.
3. Te pedirá:
   - **Nombre** (el que ve el usuario): por ejemplo `Cuadra`.
   - **Username** (debe terminar en `bot` y ser único): por ejemplo `CuadraHabanaBot`.
4. BotFather responde con algo así:

   ```
   Use this token to access the HTTP API:
   1234567890:AAExampleExampleExampleExampleExample
   ```

   **Ese es el token.** Cópialo tal cual (con el `:` en medio).

### 1.1 Ajustes recomendados del bot (sigue escribiendo a @BotFather)

- `/setdescription` → breve: "Gestor de ventas de Cuadra. Pregúntame por productos y precios."
- `/setabouttext` → lo mismo corto.
- `/setuserpic` → sube el logo (`branding.md`, avatar circular).
- `/setcommands` → pega:
  ```
  start - Empezar
  ```
- `/setjoingroups` → **Enable** (lo necesitaremos en el Hito 7 para difusiones en grupos).
- `/setprivacy` → **Disable** si en algún momento el bot irá en grupos y necesitas leer mensajes; para 1:1 no importa.

> **`/revoke`**: si algún día filtras el token, cambia el token con `/revoke` y actualiza `CUADRA_TELEGRAM_TOKEN`.

---

## 2. Identificar tu ID de Telegram (para el TCP y para pruebas)

El bot necesita **números de chat**, no usernames.

1. Abre el chat con **`@userinfobot`** (bot oficial de consulta).
2. Pulsa **Start**. Te responde tu `Id` (por ejemplo `123456789`).
3. Anota tu **ID de cliente** y, entrando con la cuenta del TCP, el **ID del TCP**.

> Alternativa sin terceros: más adelante, cuando el bot ya corra, el ID del cliente se guarda solo en `customer_channels` en el primer mensaje.

---

## 3. Arrancar Cuadra con el token

El token ya está en `backend/.env` (gitignored) y `config.py` lo carga solo al arrancar. Basta:

```powershell
# desde backend/
.\.venv\Scripts\python.exe -m uvicorn cuadra.main:app --host 127.0.0.1 --port 8321
```

Debe verse en el log:

```
cuadra.telegram: telegram polling started
```

Si en su lugar aparece `channel in dummy mode (development)`, el `.env` no se leyó: comprueba que `backend/.env` existe (cópialo de `backend/.env.example`). Las variables de entorno reales tienen prioridad sobre `.env`.

---

## 4. Probar el Hito 4 (conversación con cliente)

Desde **tu cuenta personal**, busca el bot por su `@username` y hablemos:

1. `/start`.
2. `hola, cuanto esta el cable usb-c?`  → debe responder precio + moneda.
3. `tienen el power bank de 10000?` → producto sin stock, debe responder con honestidad.
4. Manda una **nota de voz**: "buenas, me llevo dos cargadores" → primero se transcribe (STT) y luego responde.
5. Manda una **foto** → se guarda y sigue el flujo.

Reglas que deben cumplirse:
- Nunca responde en <1 s (delay humano 1–4 s; 5–15 s fuera del horario 8–21 h de La Habana).
- Aparece el "escribiendo…" antes del mensaje.
- Nada de comisiones/márgenes internos en las respuestas.

### 4.1 Comprobar en la base de datos

```powershell
# desde backend/
.\.venv\Scripts\python.exe -c "import sqlite3;c=sqlite3.connect('data/cuadra.db');c.row_factory=sqlite3.Row;print([dict(r) for r in c.execute('SELECT id,direction,author,type,body FROM messages ORDER BY id DESC LIMIT 10')])"
```

Deben estar los INBOUND del cliente y los OUTBOUND del bot, con `latency_ms`.

O por la API de debug (si `CUADRA_DEBUG=1`):

```powershell
curl -H "X-PIN: 0000" http://127.0.0.1:8321/api/debug/conversation/1
```

---

## 5. Registrar el TCP de prueba (Hito 5, dejarlo listo)

**Ya hecho**: `suppliers.id=1` tiene `telegram_id = 7516390666` (`@PeanutEmperor`) con los 3 productos semilla (Cargador, Cable, Power Bank). Si necesitas otro TCP, dos formas:

**a) Por la API de catálogo** (más adelante desde el dashboard):

```powershell
curl -X POST http://127.0.0.1:8321/api/suppliers -H "X-PIN: 0000" `
     -H "Content-Type: application/json" `
     -d '{\"name\": \"TCP Prueba\", \"business_alias\": \"Prueba\", \"telegram_id\": \"7516390666\", \"delivery_zone\": \"La Habana\"}'
```

**b) Directo en SQLite** (para una prueba rápida):

```powershell
.\.venv\Scripts\python.exe -c "import sqlite3;c=sqlite3.connect('data/cuadra.db');c.execute('INSERT INTO suppliers (name,telegram_id,delivery_zone) VALUES (?,?,?)',('TCP Prueba','7516390666','La Habana'));c.commit();print('supplier id', c.execute('SELECT last_insert_rowid()').fetchone()[0])"
```

> Para que el Hito 5 funcione, el **bot debe poder escribirle al TCP por chat privado**. Eso significa: entrar con la cuenta `@PeanutEmperor` al bot y pulsar Start una vez. El Hito 5 aún no está implementado; esto solo deja los datos listos.

---

## 6. Errores típicos y cómo se ven

| Síntoma | Causa probable | Solución |
|---|---|---|
| Log: `channel in dummy mode` | `CUADRA_TELEGRAM_TOKEN` vacío | Salir de la consola y volver a arrancar con la variable puesta |
| Log: `Unauthorized` / 401 en polling | Token mal copiado | Repite `/token` con @BotFather |
| El bot no contesta *aunque el log dice* `telegram polling started` | Bug ya corregido: `start_polling` se lanzaba sin `asyncio.create_task`, así que no hacía long-poll real. Tras el fix el log muestra `aiogram.dispatcher: Start polling` y `Run polling for bot @…` | Actualiza el código; el arranque debe verse así: `cuadra.telegram: telegram polling started` **seguido de** `aiogram.dispatcher: Run polling` |
| Log: `another cuadra-core is already running (pid …)` | Hay otro core activo (dos pollings matarían los updates) | Para el otro proceso; el guard libera solo un lock obsoleto |
| Responde "no tengo productos" | Catálogo vacío | Carga productos (`/api/products` o dashboard) — hay 3 semilla |
| Dos respuestas seguidas | Dos instancias del core activas | El guard de instancia única (abajo) ya lo impide; si pasa, mata la duplicada |
| Telegram 429 | Demasiados mensajes seguidos | Es normal en difusiones; el worker reintenta con backoff |

### 6.1 Instancia única (guard)

Cuadra **no puede** correr dos veces sobre la misma BD: ambas instancias harían `getUpdates` del mismo bot y Telegram entrega cada update a **una sola** conexión, así que los mensajes se perderían al azar. Desde el Hito 4:

- Al arrancar, `cuadra/instance.py` toma un lock en `data/cuadra.lock` (contiene el PID).
- Si ya hay un core vivo, el segundo **falla al arrancar** con un mensaje claro (no hay polling fantasma).
- Un lock obsoleto de un crash se detecta (PID muerto) y se **reclama automáticamente**.

Diagnóstico rápido:

```powershell
# desde backend/
Get-Content data/cuadra.lock   # PID del core activo (2ª línea: timestamp)
```

---

## 7. Checklist final

- [x] Bot creado (`@CuadraHabanaBot`); token en `backend/.env` (nunca en git).
- [x] `@username` del bot conocido.
- [x] ID personal (cliente) anotado: `1267804486`.
- [x] Segunda cuenta para el TCP (`@PeanutEmperor`, `7516390666`) y fila en `suppliers`.
- [ ] Core arrancado: log muestra `telegram polling started`.
- [ ] Conversación real: texto, voz y foto responden con delay humano.
- [ ] Mensajes visibles en `messages` y en `/api/debug/conversation/{id}`.
- [ ] Cuenta del TCP pulsó Start al bot al menos una vez (para el Hito 5).

---

## 8. Para después (no ahora)

- **Diffusiones (Hito 7):** el bot debe estar habilitado para grupos (`/setjoingroups enable`) y añadido a cada grupo; el `chat_id` de cada grupo se registra desde el dashboard.
- **Userbot (Telethon):** solo si algún grupo no admite bots. Requiere cuenta normal + `api_id`/`api_hash` de https://my.telegram.org. Empezar sin esto.
