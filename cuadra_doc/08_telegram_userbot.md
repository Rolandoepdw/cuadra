---
tipo: setup
proyecto: Cuadra
estado: operativo
actualizado: 2026-10-26
tags: [telegram, userbot, telethon, setup]
---

# 08 — Userbot de Telegram (Telethon)

Cuenta secundaria para publicar difusiones en grupos y atender los 1:1 que entran por esa misma cuenta (la respuesta sale por el userbot). Bot: `07_telegram_bot.md`.

## 1. Crear la app de API

1. Entra a https://my.telegram.org con la cuenta secundaria.
2. Abre **API development tools**.
3. Crea la app:
   - **App title:** `Cuadra`
   - **Short name:** `cuadra` (o `cuadra2` si está tomado)
   - **Platform:** `Other`
4. Copia **App api_id** y **App api_hash**.

Cada número admite una sola app. No hay opción para borrar solo la app ni resetear el `api_hash`.

## 2. Configurar `backend/.env`

Rellena las líneas preparadas:

```env
CUADRA_TELETHON_API_ID=12345678
CUADRA_TELETHON_API_HASH=0123456789abcdef0123456789abcdef
CUADRA_TELETHON_PHONE=+5355555555
```

Sin comillas ni espacios. Opcional:

```env
CUADRA_TELETHON_SESSION=data/telegram_user
```

No pegues estos valores en `.env.example` ni en git.

## 3. Login

Con el core detenido:

```powershell
cd C:\ruta\AProjects\cuadra\backend
.\.venv\Scripts\python.exe scripts\telegram_login.py
```

Introduce el código de Telegram y la contraseña 2FA si aplica.

Resultado esperado:

```text
Sesion creada: @usuario (id 123456789)
Archivo: ...\backend\data\telegram_user.session
```

## 4. Arrancar y verificar

```powershell
cd C:\ruta\AProjects\cuadra
.\start_cuadra.ps1 -Background
```

En **Salud** debe aparecer `Userbot: @usuario`, `Conectado: sí`, `Sesión: existe`.

Por API:

```powershell
Invoke-RestMethod http://127.0.0.1:8321/api/health -Headers @{ "X-PIN" = "0000" } |
  Select-Object -ExpandProperty telegram_user
```

## 5. Probar una difusión

1. Agrega la cuenta al grupo de prueba.
2. Dashboard → **Grupos** → **Nuevo grupo**.
3. **Cuenta de envío:** `Userbot (Telethon)` (default).
4. **Destino:** `@usuario`, ID `-100...` o link `https://t.me/+...`.
5. Dashboard → **Difusiones** → crea la campaña con ese grupo.
6. Pulsa **Programar ahora** y confirma en **Difusiones** el post en `QUEUED` → `SENT`.

> La campaña es recurrente: cada `repeat_window_days` días (panel Estrategia) se
> republica `repeat_passes` veces por grupo, respetando `max_posts_per_day` y el
> intervalo en segundos del grupo. Al completar el ciclo queda `SCHEDULED` hasta
> el siguiente inicio de ventana. El botón **Reevaluar** (en Difusiones) fuerza
> esa re-evaluación al instante, sin esperar la fecha programada: si subiste
> `repeat_passes` y aún hay cupo en el ciclo actual, publica ya.

> Solo grupos y canales. Las comunidades de Telegram no son destinos publicables y no se registran.

## 5.1. Atender 1:1 (privados)

El userbot escucha los mensajes privados entrantes (`NewMessage ... is_private`),
transcribe audio/imagen y responde por la misma cuenta:

1. Escribe un privado al userbot desde otra cuenta.
2. En el log debe aparecer el ingreso y, en **Conversaciones**, el chat con
   identidad `userbot`.
3. La respuesta sale del userbot, no del bot.

> El bot sigue atendiendo los chats que entran por el bot. La identidad
> (`conversations.identity`) evita que un mismo chat salga por la cuenta
> equivocada.

## 6. Cambiar de cuenta

1. Detén el core:

   ```powershell
   cd C:\ruta\AProjects\cuadra
   .\stop_cuadra.ps1 -CoreOnly
   ```

2. Revoca y borra la sesión:

   ```powershell
   cd backend
   .\.venv\Scripts\python.exe scripts\telegram_logout.py
   ```

3. Sustituye `CUADRA_TELETHON_API_ID`, `CUADRA_TELETHON_API_HASH` y `CUADRA_TELETHON_PHONE` en `backend/.env`.
4. Repite los pasos 3 y 4.

## 7. Desactivar el userbot

1. Comenta o elimina `CUADRA_TELETHON_*` en `backend/.env`.
2. Reinicia el core.
3. Opcional: ejecuta `scripts/telegram_logout.py`.

## 8. Límites de Telegram (anti-flood)

Los `FloodWait` los impone Telegram por cuenta. Cuadra los respeta en lugar de
insistir:

- `FloodWaitError.seconds` es el tiempo real de espera. La cola aparca el job
  hasta que expire (más un jitter) y **no consume intentos** (`attempts`).
- El cooldown es de la cuenta (`sender_state`): un castigo en un grupo detiene
  **todos** los envíos del userbot, no solo el job que falló.
- Después de cada envío se guarda `last_action_at`; hay un gap global de 8 s
  (`CUADRA_USERBOT_MIN_SEND_GAP_S`) entre envíos, además del intervalo del grupo.
- `PeerFloodError` bloquea la cuenta 1 h (`CUADRA_TELEGRAM_PEER_FLOOD_COOLDOWN_S`).
- La primera resolución de un grupo guarda su `peer_id` en `broadcast_groups`:
  los envíos siguientes no vuelven a llamar `ImportChatInviteRequest`.
- Si un job de difusión muere, su post queda `FAILED`, nunca `QUEUED` para
  siempre.

Variables de entorno:

| Variable | Default | Uso |
| --- | --- | --- |
| `CUADRA_USERBOT_MIN_SEND_GAP_S` | `8` | Gap global entre envíos |
| `CUADRA_TELEGRAM_PEER_FLOOD_COOLDOWN_S` | `3600` | Bloqueo tras `PeerFloodError` |
| `CUADRA_QUEUE_RATE_LIMIT_MAX_WAIT_S` | `86400` | Espera máxima que se respeta |
| `CUADRA_QUEUE_RATE_LIMIT_MAX_HITS` | `20` | Rate limits antes de rendirse |
| `CUADRA_QUEUE_RATE_LIMIT_JITTER_S` | `30` | Jitter máximo al reprogramar |

Para ver el cooldown actual:

```sql
SELECT * FROM sender_state WHERE sender = 'userbot';
```

## 9. Errores

| Síntoma | Solución |
| --- | --- |
| `database is locked` | Detén el core antes del login |
| `authorized False` | Repite el login y completa código/2FA |
| `No se pudo abrir la sesion` | El core sigue usando el archivo |
| `FloodWait` | Normal: se respeta solo (sección 8). Si se repite, baja frecuencia/topes |
| `ChatWriteForbiddenError` | Revisa permisos, slow mode o expulsión |
| Badge desconectado | Revisa `.env`, repite login y mira el log |

## 10. Checklist

- [ ] App de API creada con la cuenta secundaria.
- [ ] `CUADRA_TELETHON_*` configurado en `backend/.env`.
- [ ] Login completado con código y 2FA.
- [ ] **Salud** muestra el userbot correcto y conectado.
- [ ] Grupo de prueba con **Cuenta de envío: Userbot (Telethon)**.
- [ ] Difusión de prueba con su post en `SENT`.
- [ ] Privado de prueba respondido por el userbot (identidad `userbot`).
