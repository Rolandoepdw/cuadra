---
tipo: setup
proyecto: Cuadra
estado: operativo
actualizado: 2026-10-21
tags: [telegram, bot, setup]
---

# 07 — Bot de Telegram

Configura el bot de clientes 1:1. Para publicar en grupos se usa el userbot (`08_telegram_userbot.md`); la identidad `Bot` queda inactiva para difusiones. Cada conversación recuerda por qué cuenta entró (`conversations.identity`) y responde por la misma; los chats que llegan al userbot se atienden con el userbot.

## 1. Crear el bot

1. Abre `@BotFather` en Telegram.
2. Envía `/newbot`.
3. Nombre: `Cuadra`.
4. Username: debe terminar en `bot` y ser único.
5. Copia el token (`1234567890:AA...`).

## 2. Guardar el token

Edita `backend/.env`:

```env
CUADRA_TELEGRAM_TOKEN=1234567890:AA...
```

Sin comillas ni espacios. No lo pongas en `.env.example` ni en git.

## 3. Ajustes del bot

En `@BotFather`:

1. `/mybots` → bot → **Edit Bot**.
2. **Edit Description**: `Gestor de ventas de Cuadra. Pregunta por productos y precios.`
3. **Edit About**: `Ventas y entregas en La Habana.`
4. **Edit Botpic**: sube el logo.
5. **Edit Commands**:
   ```
   start - Empezar
   ```
6. `/setjoingroups` → bot → **Enable**.

Si el token se filtra: `/revoke`, copia el nuevo y actualiza `.env`.

## 4. Arrancar y verificar

```powershell
cd C:\ruta\AProjects\cuadra
.\start_cuadra.ps1 -Background
Invoke-RestMethod http://127.0.0.1:8321/api/health -Headers @{ "X-PIN" = "0000" }
```

Debe aparecer `telegram_channel: polling`. En el log:

```powershell
Get-Content backend\data\logs\cuadra.log -Tail 30
```

Busca `telegram polling started` y `Run polling for bot @...`.

## 5. Probar el bot

1. Desde otra cuenta, busca el bot y pulsa **Start**.
2. Envía `hola, cuánto está el cable usb-c?`.
3. Envía una nota de voz y una foto.

Debe responder con precio/moneda, transcribir la voz y guardar la foto.

## 6. TCP de prueba

1. Abre `@userinfobot` con la cuenta del TCP y copia su `Id`.
2. Dashboard → **Catálogo** → **TCPs** → **Nuevo TCP**.
3. Nombre: `TCP Prueba`; Telegram ID: el ID copiado.
4. Entra con la cuenta del TCP al bot y pulsa **Start**.
5. Agrega productos a ese TCP.

## 7. Grupos (solo si usas `sender = bot`)

La vía normal es el userbot. Si aun así quieres usar la Bot API:

1. Agrega el bot al grupo.
2. Dale permiso para enviar mensajes.
3. Dashboard → **Grupos** → **Nuevo grupo**.
4. **Cuenta de envío:** `Bot (Bot API, inactivo)`.
5. **Destino:** `@usuario`, ID `-100...` o link de invitación.

No se registran comunidades: solo grupos y canales.

## 8. Transferir o recrear el bot

**Transferir:**

1. La cuenta nueva debe tener username y haber abierto `@BotFather`.
2. Cuenta actual: `/mybots` → bot → **Bot Settings** → **Transfer Ownership**.
3. Escribe el username de la cuenta nueva.
4. La cuenta nueva acepta la transferencia.
5. Verifica con `/mybots` desde la cuenta nueva.

**Recrear:**

1. Cuenta actual: `/mybots` → bot → **Delete Bot**.
2. Cuenta nueva: `/newbot` y copia el token.
3. Reemplaza `CUADRA_TELEGRAM_TOKEN` en `backend/.env`.
4. Repite los pasos 3 y 7.
5. Reinicia el core.

## 9. Errores

| Síntoma | Solución |
| --- | --- |
| `channel in dummy mode` | Token vacío o `.env` no cargado |
| `Unauthorized` / 401 | Token incorrecto; usa `/token` |
| `another cuadra-core is already running` | Detén el otro core |
| El bot no responde | Revisa el log y el catálogo |
| Telegram 429 | Baja la frecuencia; el worker reintenta |

## 10. Checklist

- [ ] Bot creado y token en `backend/.env`.
- [ ] Descripción, about, foto y comandos configurados.
- [ ] `/setjoingroups` en Enable.
- [ ] Core arrancado y `telegram_channel: polling`.
- [ ] Prueba de texto, voz y foto.
- [ ] TCP de prueba registrado y con Start.
- [ ] Bot agregado a los grupos.
