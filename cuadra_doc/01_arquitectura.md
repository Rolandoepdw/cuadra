---
tipo: arquitectura
proyecto: Cuadra
estado: diseño + implementación (Hitos 1–3)
actualizado: 2026-10-19
tags: [arquitectura, backend, colas, dashboard, windows]
---

# 01 — Arquitectura Técnica (Fase 1)

> Base de decisiones: `00_decisiones.md`. Este documento describe *cómo* se materializa lo decidido.

## 1. Vista general

```
[Tu laptop — Windows]
│
│  ┌──────────────────────────────────────────────────────────────┐
│  │  cuadra-core  (un solo proceso Python: FastAPI + asyncio)    │
│  │                                                              │
│  │   Telegram (aiogram, polling) ◄──────────┐                   │
│  │   API Dashboard (REST + PIN) ◄───────────┤                   │
│  │   Scheduler broadcasts (APScheduler) ◄───┤   entradas        │
│  │                                          ▼                   │
│  │   ┌──────────── cola persistente (jobs en SQLite) ──────────┐  │
│  │   │  transcribe_audio → generate_reply → send             │  │
│  │   └──────────────────────────┬────────────────────────────┘  │
│  │                              ▼                               │
│  │   domain/  catalog · conversations · escalations · sales     │
│  │            broadcasts · events                               │
│  └──────────────────────────┬───────────────────────────────────┘
│                             │ HTTP local
│        ┌────────────────────┼────────────────────┐
│        ▼                    ▼                    ▼
│   llama.cpp (GPU, :8080)  faster-whisper      SQLite (WAL)
│   server propio de Cuadra (en proceso, CPU)   cuadra.db + fotos/
│   Bonsai-8B-Q1_0 ~2.4 GB  4 hilos
│
│   Dashboard Flutter Web  →  build estático servido por FastAPI
│   (acceso desde móvil/laptop en la misma red local, con PIN)
│
[Fase 2] gateway WhatsApp (proceso Node nativo en Windows, sin Docker)
         → webhook hacia cuadra-core en el mismo puerto FastAPI
```

## 2. Estructura del monorepo

```
cuadra/
├── cuadra_doc/                # esta documentación
├── backend/
│   ├── cuadra/
│   │   ├── main.py           # app FastAPI + lifespan (arranca todo)
│   │   ├── config.py         # settings (rutas, PIN, LLM) + carga de .env
│   │   ├── db.py             # conexión SQLite, WAL, migraciones
│   │   ├── instance.py       # guard de instancia única (evita pollings duplicados)
│   │   ├── domain/
│   │   │   ├── catalog.py        # suppliers (TCP), products, images, categories
│   │   │   ├── conversations.py  # customers, sesiones, mensajes, contexto
│   │   │   ├── events.py         # auditoría (tabla events)
│   │   │   ├── errors.py         # errores de dominio
│   │   │   └── (escalations, sales, broadcasts)  # Hitos 5/7/8
│   │   ├── channels/
│   │   │   ├── __init__.py       # registry de adaptadores (get_adapter)
│   │   │   ├── base.py           # interfaz Channel (Protocol)
│   │   │   └── telegram.py       # aiogram: polling (entrada) + send_* (salida)
│   │   ├── ai/
│   │   │   ├── queue.py          # worker persistente + prioridades
│   │   │   ├── handlers.py       # generate_reply, transcribe_audio, send_message
│   │   │   ├── ingest.py         # entrada de mensajes: guardar + encolar trabajo
│   │   │   ├── pipeline.py       # NLU → reglas/plantillas → respuesta
│   │   │   ├── llm.py            # cliente LLM (OpenAI-compatible | Ollama)
│   │   │   ├── llm_server.py     # supervisor de llama.cpp propio
│   │   │   ├── prompts.py        # persona, tareas y plantillas
│   │   │   └── stt.py            # faster-whisper (thread executor)
│   │   ├── api/
│   │   │   ├── auth.py           # PIN (header X-PIN)
│   │   │   ├── models.py         # esquemas de entrada (Pydantic)
│   │   │   ├── routes_dashboard.py  # salud + resumen
│   │   │   ├── routes_catalog.py    # CRUD catálogo
│   │   │   ├── routes_llm.py        # start/stop/restart del LLM
│   │   │   └── routes_debug.py      # endpoints de desarrollo
│   │   └── scheduler/
│   │       └── broadcasts.py    # APScheduler + jitter (Hito 7)
│   ├── migrations/             # SQL numerados (001_init.sql, …)
│   ├── models/                 # GGUF (gitignored)
│   ├── tools/                  # llama.cpp (gitignored)
│   ├── scripts/                # install_llm.ps1, start_llm.ps1, lab/
│   ├── data/                   # cuadra.db, fotos/, logs/ (gitignored)
│   └── requirements.txt
└── dashboard/                  # flutter create (plantilla web)
    └── lib/
        ├── main.dart
        ├── api/                # cliente HTTP + modelos
        └── pantallas/          # los 7 paneles
```

**Regla de dependencia:** `channels/` y `api/` dependen de `domain/`; `domain/` no conoce a `channels/` ni a FastAPI. `ai/` es consumido por el worker de cola. Esto es lo que permite añadir el canal WhatsApp (Fase 2) sin tocar dominio.

## 3. Adaptador de canal (interfaz)

```python
class Channel(Protocol):
    async def send_text(self, target: str, text: str) -> None: ...
    async def send_image(self, target: str, path: str, caption: str | None) -> None: ...
    async def send_audio(self, target: str, path: str) -> None: ...
    async def typing(self, target: str) -> None:  # "typing…" si el canal lo soporta

Los mensajes entrantes se normalizan a un evento interno (`channel`, `identifier`,
`type`, `body_or_path`, `sender`) — del cual cuelga todo el pipeline de negocio.
**Fase 2 = implementar `channels/whatsapp.py` y registrarla; nada más.**

## 4. Pipeline de un mensaje entrante (cliente)

```
entrada (polling/webhook) → se guarda en `messages` y se encola
  │
  ├─ si es audio → JOB{transcribe_audio}  (faster-whisper, CPU)
  │       └─ transcripción → JOB{generate_reply}
  │
  ├─ si es imagen → se guarda en disco + sigue el mismo camino que el texto
  │
  └─ JOB{generate_reply}
        ├─ historial de la conversación (últimos N mensajes de SQLite)
        ├─ contexto: producto en discusión, stock, precio, TCP (supplier)
        ├─ LLM (llama.cpp) → intención + parámetros (JSON)
        ├─ el pipeline decide: plantilla para los casos comunes, generación
        │  libre (con JSON real del catálogo) solo para comparar/queja/otro
        ├─ respuesta guardada como mensaje OUTBOUND → JOB{send_message}
        └─ si falta un dato → JOB de escalado al TCP (Hito 5)
              └─ respuesta TCP → enriquece especificaciones_json → retoma cliente

JOB{send_message} → typing + delay humano → adaptador de canal
  (`channels.get_adapter`) → send_text / send_image / send_audio
```

El destino de salida se resuelve desde `customer_channels` (`get_channel_identifier`):
el mensaje guardado no conoce la dirección, el canal la aporta.

## 5. Concurrencia y colas (punto crítico)

- **Una sola cola FIFO persistente** (`jobs` en SQLite): sobrevive a apagones; al arrancar se reanudan pendientes.
- **Instancia única obligatoria:** dos cores harían `getUpdates` del mismo bot y Telegram entrega cada update a una sola conexión (mensajes perdidos al azar). `cuadra/instance.py` toma un lock (`data/cuadra.lock`) al arrancar y aborta el segundo core; los locks obsoletos se reclaman solos. El polling de aiogram **debe** programarse con `asyncio.create_task` o no hace long-poll real.
- **Worker de IA con concurrencia 1 en GPU** (`-np 1` en el server llama.cpp): mientras un prompt corre, los demás esperan en la cola. Con 2–4 usuarios el sentimiento sigue siendo humano.
- **Whisper corre en `run_in_executor`** (4 hilos CPU, no bloquea el event loop). Máximo 1 transcripción a la vez para no pelear por CPU con el LLM.
- **Prioridades:** respuestas a clientes (10) > consultas a TCP (50) > difusiones (100).
- **Timeout de escalado TCP:** si no hay respuesta en N minutos, se reconsulta una vez y luego se resuelve con el cliente honestamente ("el socio está ocupado, te confirmo apenas me responda"). Nunca dejar la conversación colgada.
- **Delays humanos** aplicados en la salida (job `send_message`): typing de 1–4 s + jitter; fuera de horario (8–21 h, America/Havana) 5–15 s; en difusiones, 3–40 min entre posts. Nunca <1 s.
- **Idempotencia tras apagón:** al arrancar, los trabajos en `IN_PROGRESS` se devuelven a `PENDING` con `attempts + 1` (evento `jobs_requeued`). Antes de enviar un mensaje saliente, el worker comprueba si ya existe un evento `message_sent` para ese mensaje; si existe, el trabajo se marca COMPLETED sin reenviar. Queda una ventana residual mínima de doble envío (crash exacto entre el envío y el registro del evento): aceptable para 1:1 y documentada.
- **Logging:** `logging` con RotatingFileHandler en `data/logs/cuadra.log` (5 MB × 5 archivos). Nivel INFO en producción; DEBUG con variable de entorno. Todo trabajo fallido deja `last_error` + fila en `events`.
- **Errores del worker:** transitorios (red, modelo caído, Telegram 429) → reintento con backoff exponencial hasta `max_intentos`, luego FALLIDO. Permanentes (payload inválido, producto dado de baja) → FALLIDO directo + evento `job_failed`. El worker nunca muere sin marcar el trabajo en curso.
- **Rate limits de canal:** Telethon duerme el `FloodWait` que Telegram pida; la Bot API respeta `RetryAfter`. Ambos aplican también a difusiones (no solo 1:1).

## 6. llama.cpp y Whisper — configuración operativa

| Ajuste | Valor | Por qué |
|---|---|---|
| Gestión del server | Cuadra lo arranca/para solo (`ai/llm_server.py`: autostart + stop al cerrar) | Sin apps externas; libera VRAM al salir |
| Concurrencia | `-np 1` | 4 GB VRAM: una inferencia a la vez; dos provocan swap a CPU |
| Modelo | `Bonsai-8B-Q1_0.gguf` | ~2.4 GB VRAM @8K; mejor NLU del bake-off (`06_bakeoff_modelos.md`) |
| GPU / contexto | `-ngl 99`, `-c 8192` | Todo el modelo en GPU; ventana de 8K tokens |
| Contexto de prompt | presupuesto ~2–2.5K tokens | historial acotado (últimos ~12 mensajes + resumen + producto) |
| Whisper | `small` int8, CPU, 4 hilos (validar con audios reales) | VRAM intacta para el LLM. `large` descartado en CPU. Ver §6.2 |

**Estrategia de respuesta:** el LLM **no redacta libre** para todo. Intención + slots en JSON; la respuesta comercial se arma desde **plantillas con ranuras** (precio, zona, stock, moneda) + retoque del LLM solo cuando hace falta. Rápido, consistente con la voz de marca y menos alucinaciones.

### 6.1 Gestión de la ventana de contexto del LLM

El backend **arma el prompt, no lo acumula**. Presupuesto objetivo: **~2–2.5K tokens** por llamada (latencia estable con el modelo por defecto y 4 GB de VRAM):

| Componente | Contenido | ~Tokens |
|---|---|---|
| System prompt + few-shots | `03_persona_prompts.md` (versión compacta) | 600–800 |
| Resumen rodante | Último resumen generado (cada ~10 mensajes se comprime la historia vieja en 2–3 líneas) | 100–150 |
| Historial reciente | Últimos ~12 mensajes de `mensajes` | 800–1000 |
| Contexto del producto | JSON estructurado: precio, moneda, stock, zona TCP, `especificaciones_json` | 150–300 |
| Mensaje actual + tarea | Último mensaje + instrucción de NLU/respuesta | 50–150 |

Reglas:
- **NLU no usa historial completo**: clasificar intención = último mensaje + producto activo.
- **El resumen rodante se persiste** en `conversations.context_json.summary` — sobrevive a reinicios.
- Si el historial supera el presupuesto, se recorta por la cola (más viejo afuera, resumen cubre lo esencial).
- Escalar el modelo (más parámetros) no está en el radar; gestionar contexto bien resuelve el 90% de los casos.

### 6.2 Calidad de transcripción (whisper)

| Modelo (CPU, int8, 4 hilos) | Velocidad aprox. 30 s audio | Calidad español | Veredicto |
|---|---|---|---|
| base | ~4–8 s | Regular: falla números y marcas | Descartado como default |
| **small** | ~8–15 s | Buena | **Default** |
| medium | ~20–40 s | Muy buena | Upgrade si CPU aguanta |
| large-v3 | minutos | Excelente | Descartado en CPU |

- **`initial_prompt` con nombres del catálogo** (faster-whisper lo soporta): inyectar marcas/productos frecuentes ("EcoFlow, panel solar, CUP, USD…") mejora mucho el reconocimiento de marcas, números y precios — el punto donde small falla.
- **Criterio de aceptación:** probar con 10 notas de voz reales (cubano, ruido de fondo); si números/marcas fallan >2 de 10, subir a `medium` o reforzar el `initial_prompt`.
- Camino futuro (no Fase 1): si en algún momento hay VRAM holgada (carga del LLM baja), `large-v3` int8_float16 en GPU (~1–1.5 GB) vía ctranslate2 — decidir midiendo, no por supuesto.

## 7. Despliegue en Windows (sin Docker, sin WSL2)

- `cuadra-core`: proceso Python venv, arrancado con `pythonw` / tarea programada al inicio de sesión (o NSSM como servicio).
- Motor LLM: **llama.cpp propio** (build upstream, MIT) en `backend/tools/llamacpp/`, con el GGUF en `backend/models/` (única descarga grande, vía hotspot, una sola vez). Instalado por `backend/scripts/install_llm.ps1`. **No se usa Ollama.**
- faster-whisper: corre dentro del proceso Python (weights en disco, sin servidor).
- **Fase 2 (WhatsApp):** evaluar en este orden:
  1. Gateway `whatsapp-web.js` en Node nativo Windows (playwright/chromium headless) — sin Docker.
  2. Evolution API instalada como app Node nativa (posible pero menos soportado fuera de Docker).
  3. *Último recurso:* WSL2 solo para ese componente.
  La decisión se toma al arrancar Fase 2 con una prueba de baneo controlada en la línea secundaria.
- **Red:** sin puertos abiertos al exterior. Telegram por polling. Dashboard solo escucha en `0.0.0.0` local protegido por PIN (LAN).

### 7.1 Mantenimiento y backups

- **Backup diario** (tarea programada de Windows):
  1. `sqlite3 cuadra.db ".backup backups\cuadra_YYYYMMDD.db"` — copia consistente compatible con WAL (nunca copiar el archivo a pelo mientras corre).
  2. `robocopy data\fotos backups\fotos /MIR`
  - Retener los últimos 7 días. `backups/` puede vivir en un disco externo.
- **Integridad:** al arrancar, `PRAGMA quick_check`; si falla, restaurar desde el último backup bueno y dejar evento `corrupcion_bd` en `eventos`.
- **Mantenimiento SQLite:** `VACUUM` manual mensual (BD pequeña, <1 min). Monitorear el tamaño de `cuadra.db-wal` en el panel Salud (si crece descontrolado, falta un checkpoint).
- **Límites de tamaño:** imágenes de catálogo comprimidas a máx. 1280 px / ~500 KB al subir. Audios de clientes limitados a 2 min.

## 8. Dashboard Flutter Web

- `flutter build web` → copiado a `backend/cuadra/web_dashboard/` y servido por FastAPI como `StaticFiles`.
- **¿Web y no Desktop?** El proyecto Flutter es idéntico para ambos targets. Web primero: acceso desde el móvil en la LAN sin instalar nada, y cero despliegue extra (los estáticos los sirve el FastAPI que ya corre). Si algún día se quiere `.exe` local: `flutter build windows` sobre el mismo `lib/` — sin reescritura.
- API REST bajo `/api/*` con header `X-PIN`.
- **Paneles (Fase 1):**

| Panel | Contenido | Escritura |
|---|---|---|
| General | mensajes hoy, conversaciones activas, latencia media LLM/STT, cola | — |
| Conversaciones | historial en vivo, estados, duración, escalados abiertos | — |
| Catálogo | CRUD TCPs → productos → imágenes (subida) | ✅ |
| Grupos | CRUD de grupos de difusión (canal, destino, topes, intervalos) y categorías; asignar categorías aceptadas por grupo y categoría por producto | ✅ |
| Difusiones | programadas, próximas (jitter visible), enviadas, fallidas, y **log de envíos por grupo** | programar/cancelar |
| Auditoría | eventos del sistema, jobs fallidos, reintentos | — |
| Salud | RAM/VRAM, uptime, estado del LLM (llama.cpp: corriendo/modelo), tamaño de BD | — |

- **Curaduría de grupos = 100% operador (nunca el bot):** el panel Grupos es donde se registran grupos manualmente (nombre, invite/chat id, categorías permitidas, tope diario, intervalos). El scheduler solo publica en grupos activos cuya categoría coincida con la del producto; producto sin categoría → solo grupos sin restricción. Ver `02_modelo_datos.md` §3 y `00_decisiones.md` §7.1.
- Endpoints mínimos: `GET /api/health`; CRUD de catálogo `GET/POST/PATCH/DELETE /api/categories`, `/api/suppliers`, `/api/products` (+ `POST /api/products/{id}/images`, `PUT /api/products/{id}/main-image`, `DELETE /api/products/{id}/images/{image_id}`); LLM `GET /api/llm/status`, `GET /api/llm/models`, `POST /api/llm/start|stop|restart`; y (Hitos 5–8) `GET /api/conversations`, `GET/POST/PATCH/DELETE /api/broadcast-groups` y `/api/categories`, `GET/POST /api/broadcasts`, `GET /api/broadcast-posts`, `GET /api/audit`, `GET /api/sales` (Fase 1 solo lectura).

## 9. Riesgos conocidos

| Riesgo | Mitigación |
|---|---|
| Apagón a mitad de conversación | Cola y estados en SQLite; al arrancar se reanudan; el cliente ve silencio (aceptable) |
| Baneo de línea WA (Fase 2) | Política anti-ban (`00_decisiones.md` §5); número secundario rotable |
| VRAM insuficiente (app personal + LLM) | `-np 1`; cerrar apps pesadas (p. ej. LM Studio); fallback a `backend/tools/llamacpp-cpu/` de emergencia |
| El modelo suena robot | Plantillas + few-shots en `03_persona_prompts.md`; auditar en panel Conversaciones |
| SQLite contention (un solo escritor) | WAL + todo escritura desde cuadra-core; dashboard escribe solo vía API del core |
| Consumo de datos del hotspot | Whisper y LLM corren 100% locales (cero datos); los datos solo se consumen al descargar modelos (una vez) y en mensajería WA real (Fase 2); imágenes comprimidas ≤500 KB |
