---
tipo: setup
proyecto: Cuadra
estado: operativo
actualizado: 2026-10-21
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
cd C:\Rolando\AProjects\cuadra\backend
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
cd C:\Rolando\AProjects\cuadra
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
> el siguiente inicio de ventana.

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
   cd C:\Rolando\AProjects\cuadra
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

## 8. Errores

| Síntoma | Solución |
| --- | --- |
| `database is locked` | Detén el core antes del login |
| `authorized False` | Repite el login y completa código/2FA |
| `No se pudo abrir la sesion` | El core sigue usando el archivo |
| `FloodWait` | Baja frecuencia/topes; el worker reintenta |
| `ChatWriteForbiddenError` | Revisa permisos, slow mode o expulsión |
| Badge desconectado | Revisa `.env`, repite login y mira el log |

## 9. Checklist

- [ ] App de API creada con la cuenta secundaria.
- [ ] `CUADRA_TELETHON_*` configurado en `backend/.env`.
- [ ] Login completado con código y 2FA.
- [ ] **Salud** muestra el userbot correcto y conectado.
- [ ] Grupo de prueba con **Cuenta de envío: Userbot (Telethon)**.
- [ ] Difusión de prueba con su post en `SENT`.
- [ ] Privado de prueba respondido por el userbot (identidad `userbot`).
