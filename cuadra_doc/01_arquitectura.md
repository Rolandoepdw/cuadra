---
tipo: arquitectura
proyecto: Cuadra
estado: diseño Fase 1
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
│  │   Scheduler difusiones (APScheduler) ◄────┤   entradas        │
│  │                                          ▼                   │
│  │   ┌──────────── cola persistente (trabajos en SQLite) ─────┐  │
│  │   │  transcribir_audio → clasificar → responder → enviar  │  │
│  │   └──────────────────────────┬────────────────────────────┘  │
│  │                              ▼                               │
│  │   dominio/  catalogo · conversaciones · escalado · ventas    │
│  │             difusiones · auditoria                           │
│  └──────────────────────────┬───────────────────────────────────┘
│                             │ HTTP local
│        ┌────────────────────┼────────────────────┐
│        ▼                    ▼                    ▼
│   Ollama (GPU, :11434)   faster-whisper      SQLite (WAL)
│   qwen2.5:3b q4          (en proceso, CPU)   cuadra.db + fotos/
│   ~2.2 GB VRAM           4 hilos, ~500 MB
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
│   │   ├── config.py         # settings (rutas, PIN, modelo, etc.)
│   │   ├── db.py             # conexión SQLite, WAL, migraciones
│   │   ├── dominio/
│   │   │   ├── catalogo.py       # TCPs, productos, imágenes
│   │   │   ├── conversaciones.py # sesiones, mensajes, contexto
│   │   │   ├── escalado.py       # bucle TCP (espera/resolución/timeout)
│   │   │   ├── ventas.py         # borradores, aprobación, comisiones
│   │   │   ├── difusiones.py     # programación con jitter/spintax
│   │   │   └── auditoria.py      # eventos
│   │   ├── canales/
│   │   │   ├── base.py           # interfaz Canal (enviar/recibir/media)
│   │   │   └── telegram.py       # implementación aiogram
│   │   ├── ia/
│   │   │   ├── cola.py           # worker FIFO + prioridades
│   │   │   ├── llm.py            # cliente Ollama (NLU + generación)
│   │   │   └── stt.py            # faster-whisper (thread executor)
│   │   ├── api/
│   │   │   ├── auth.py           # PIN (header X-PIN o cookie)
│   │   │   └── rutas_dashboard.py# REST para el panel
│   │   └── scheduler/
│   │       └── difusiones.py    # APScheduler + jitter
│   ├── migrations/             # SQL numerados (001_init.sql, …)
│   ├── data/                   # cuadra.db, fotos/ (gitignored)
│   └── requirements.txt
└── dashboard/                  # flutter create (plantilla web)
    └── lib/
        ├── main.dart
        ├── api/                # cliente HTTP + modelos
        └── pantallas/          # los 7 paneles
```

**Regla de dependencia:** `canales/` y `api/` dependen de `dominio/`; `dominio/` no conoce a `canales/` ni a FastAPI. `ia/` es consumido por el worker de cola. Esto es lo que permite añadir el canal WhatsApp (Fase 2) sin tocar dominio.

## 3. Adaptador de canal (interfaz)

```python
class Canal(Protocol):
    async def enviar_texto(self, destino: str, texto: str) -> None: ...
    async def enviar_imagen(self, destino: str, ruta: str, pie: str | None) -> None: ...
    async def enviar_audio(self, destino: str, ruta: str) -> None: ...
    async def escribiendo(self, destino: str) -> None: ...  # "typing…" si el canal lo soporta
```

Los mensajes entrantes se normalizan a un evento interno:
`MensajeEntrante(canal, identificador, tipo, cuerpo_o_ruta, remitente)` — del cual cuelga todo el pipeline de negocio. **Fase 2 = implementar `canales/whatsapp.py` y registrarla; nada más.**

## 4. Pipeline de un mensaje entrante (cliente)

```
webhook/polling → MensajeEntrante
  │
  ├─ si es audio → TRABAJO{transcribir_audio}  (whisper, CPU)
  │       └─ transcripción → TRABAJO{generar_respuesta}
  │
  ├─ si es imagen → se guarda en disco + descripción corta al contexto
  │
  └─ TRABAJO{generar_respuesta}
        ├─ historial de la conversación (últimos N mensajes de SQLite)
        ├─ contexto: producto en discusión, stock, precio, TCP asignado
        ├─ Ollama → intención + parámetros (JSON) + respuesta comercial
        └─ si necesita dato externo → TRABAJO{consultar_tcp} (pausa asíncrona)
              └─ respuesta TCP → enriquece especificaciones_json → retoma cliente
```

## 5. Concurrencia y colas (punto crítico)

- **Una sola cola FIFO persistente** (`trabajos` en SQLite): sobrevive a apagones; al arrancar se reanudan pendientes.
- **Worker de IA con concurrencia 1 en GPU** (`num_parallel=1` en Ollama): mientras un prompt corre (~0.5–1.5 s con 3B Q4), los demás esperan. Con 2–4 usuarios el sentimiento sigue siendo humano.
- **Whisper corre en `run_in_executor`** (4 hilos CPU, no bloquea el event loop). Máximo 1 transcripción a la vez para no pelear por CPU con el LLM.
- **Prioridades:** respuestas a clientes (10) > consultas a TCP (50) > difusiones (100).
- **Timeout de escalado TCP:** si no hay respuesta en N minutos, se reconsulta una vez y luego se resuelve con el cliente honestamente ("el socio está ocupado, te confirmo apenas me responda"). Nunca dejar la conversación colgada.
- **Delays humanos** aplicados en la salida: typing de 1–4 s + jitter; en difusiones, 3–40 min entre posts.
- **Idempotencia tras apagón:** al arrancar, los trabajos en `EN_PROCESO` se devuelven a `PENDIENTE` con `intentos + 1`. Cada trabajo lleva una clave en `payload_json.idempotencia` (p. ej. `escalado:{id}:respuesta`); antes de enviar un mensaje saliente, el worker verifica la clave contra `mensajes`/`envios_difusion` — si ya existe, se marca COMPLETADO sin reenviar. Queda una ventana residual mínima de doble envío (crash exacto entre verificación y envío): aceptable para 1:1 y documentada.
- **Logging:** `logging` con RotatingFileHandler en `data/logs/cuadra.log` (5 MB × 5 archivos). Nivel INFO en producción; DEBUG con variable de entorno. Todo trabajo fallido deja `error_ultimo` + fila en `eventos`.
- **Errores del worker:** transitorios (red, Ollama caído, Telegram 429) → reintento con backoff exponencial hasta `max_intentos`, luego FALLIDO. Permanentes (payload inválido, producto dado de baja) → FALLIDO directo + evento `trabajo_fallido`. El worker nunca muere sin marcar el trabajo en curso.
- **Rate limits de canal:** Telethon duerme el `FloodWait` que Telegram pida; la Bot API respeta `RetryAfter`. Ambos aplican también a difusiones (no solo 1:1).

## 6. Ollama y Whisper — configuración operativa

| Ajuste | Valor | Por qué |
|---|---|---|
| `OLLAMA_KEEP_ALIVE` | permanente | Evita recargar ~2 GB en cada consulta (segundos perdidos) |
| `num_parallel` | 1 | 4 GB VRAM: una inferencia a la vez; dos provocan swap a CPU |
| Modelo | `qwen2.5:3b-instruct-q4_K_M` | ~2.0–2.2 GB VRAM, rinde para NLU+respuesta con few-shots |
| Contexto LLM | 2048–4096 tokens | historial acotado (últimos ~12 mensajes + estado) |
| Whisper | `small` int8, CPU, 4 hilos (validar con audios reales) | VRAM intacta para Ollama. `large` descartado en CPU. Ver §6.2 |

**Estrategia de respuesta:** el LLM **no redacta libre** para todo. Intención + slots en JSON; la respuesta comercial se arma desde **plantillas con ranuras** (precio, zona, stock, moneda) + retoque del LLM solo cuando hace falta. Rápido, consistente con la voz de marca y menos alucinaciones.

### 6.1 Gestión de la ventana de contexto del LLM

El backend **arma el prompt, no lo acumula**. Presupuesto objetivo: **~2–2.5K tokens** por llamada (latencia estable con 3B y 4 GB de VRAM):

| Componente | Contenido | ~Tokens |
|---|---|---|
| System prompt + few-shots | `03_persona_prompts.md` (versión compacta) | 600–800 |
| Resumen rodante | Último resumen generado (cada ~10 mensajes se comprime la historia vieja en 2–3 líneas) | 100–150 |
| Historial reciente | Últimos ~12 mensajes de `mensajes` | 800–1000 |
| Contexto del producto | JSON estructurado: precio, moneda, stock, zona TCP, `especificaciones_json` | 150–300 |
| Mensaje actual + tarea | Último mensaje + instrucción de NLU/respuesta | 50–150 |

Reglas:
- **NLU no usa historial completo**: clasificar intención = último mensaje + producto activo.
- **El resumen rodante se persiste** en `conversaciones.contexto_json.resumen` — sobrevive a reinicios.
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
- Camino futuro (no Fase 1): si en algún momento hay VRAM holgada (carga de Ollama baja), `large-v3` int8_float16 en GPU (~1–1.5 GB) vía ctranslate2 — decidir midiendo, no por supuesto.

## 7. Despliegue en Windows (sin Docker, sin WSL2)

- `cuadra-core`: proceso Python venv, arrancado con `pythonw` / tarea programada al inicio de sesión (o NSSM como servicio).
- Ollama para Windows: instalado nativo, con el modelo ya descargado (única descarga grande, vía hotspot, una sola vez ~2 GB).
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
| Auditoría | eventos del sistema, trabajos fallidos, reintentos | — |
| Salud | RAM/VRAM, uptime, estado de Ollama, tamaño de BD | — |

- **Curaduría de grupos = 100% operador (nunca el bot):** el panel Grupos es donde se registran grupos manualmente (nombre, invite/chat id, categorías permitidas, tope diario, intervalos). El scheduler solo publica en grupos activos cuya categoría coincida con la del producto; producto sin categoría → solo grupos sin restricción. Ver `02_modelo_datos.md` §3 y `00_decisiones.md` §7.1.
- Endpoints mínimos: `GET /api/resumen`, `GET /api/conversaciones`, `GET /api/catalogo/*`, `POST/PATCH/DELETE /api/catalogo/*`, `GET/POST/PATCH/DELETE /api/grupos`, `GET/POST/PATCH/DELETE /api/categorias`, `GET /api/difusiones`, `POST /api/difusiones`, `GET /api/envios_difusion`, `GET /api/auditoria`, `GET /api/salud`, `GET /api/ventas` (Fase 1 solo lectura; aprobación de ventas llega en Fase 1.5/2 cuando existan las primeras).

## 9. Riesgos conocidos

| Riesgo | Mitigación |
|---|---|
| Apagón a mitad de conversación | Cola y estados en SQLite; al arrancar se reanudan; el cliente ve silencio (aceptable) |
| Baneo de línea WA (Fase 2) | Política anti-ban (`00_decisiones.md` §5); número secundario rotable |
| VRAM insuficiente (app personal + Ollama) | `num_parallel=1`; doc de operación: cerrar apps pesadas; fallback CPU-only de emergencia |
| LLM de 3B suena robot | Plantillas + few-shots en `03_persona_prompts.md`; auditar en panel Conversaciones |
| SQLite contention (un solo escritor) | WAL + todo escritura desde cuadra-core; dashboard escribe solo vía API del core |
| Consumo de datos del hotspot | Whisper y LLM corren 100% locales (cero datos); los datos solo se consumen al descargar modelos (una vez) y en mensajería WA real (Fase 2); imágenes comprimidas ≤500 KB |
