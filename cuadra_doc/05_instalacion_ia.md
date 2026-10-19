---
tipo: guia_instalacion
proyecto: Cuadra
estado: Fase 1 (Hito 3 · LLM propio)
actualizado: 2026-10-19
tags: [llamacpp, whisper, modelos, instalacion, entorno]
---

# 05 — Instalación y configuración de IA (llama.cpp propio + Whisper)

> Cuadra **no depende de ninguna app de LLM externa**. Trae su propio runtime de
> **llama.cpp** (MIT) en `backend/tools/llamacpp/` y lo arranca/para solo. El modelo
> corre en la GPU y se libera al cerrar la app. Whisper (STT) corre en **CPU (int8)**.

## 1. Estado detectado en la máquina (2026-10-19)

| Componente              | Estado                                                                 | Nota                                              |
| ----------------------- | ---------------------------------------------------------------------- | ------------------------------------------------- |
| llama.cpp propio        | **instalado** en `backend/tools/llamacpp/` (GPU) y `...-cpu/`           | Build `b11739`, `llama-server` `0.7.0-dev`        |
| Ollama                  | **NO instalado** (antes se asumía por Hermes)                           | No lo usa Cuadra; ver §4.5                        |
| LM Studio               | instalado                                                              | Solo **desarrollo**; puerto `1234`; ver §4.5      |
| faster-whisper `base`   | descargado completo (~141 MB) en caché HF                              | Usable con `CUADRA_WHISPER_MODEL=base`            |
| faster-whisper `small`  | descargado (tardó ~10 min por red lenta)                               | **Modelo STT por defecto**                        |
| ffmpeg                  | `C:\ffmpeg\bin\ffmpeg.exe`                                             | No lo usa la app (PyAV decodifica solo)           |
| Voces TTS de Windows    | solo `David`/`Zira` (en-US)                                            | Solo para audio de prueba                         |

GPU: **NVIDIA RTX 3050 Laptop (4096 MiB)**, driver 610.47. Presupuesto: dejar el
runtime + modelo dentro de ~2.5–3.5 GB de VRAM para no ahogar el escritorio.

## 2. Runtime propio de llama.cpp

### 2.1 Instalar / reinstalar

```powershell
cd backend
.\scripts\install_llm.ps1
```

Descarga el último build estable de llama.cpp (CUDA) y lo descomprime en
`backend/tools/llamacpp/` (y una copia CPU en `backend/tools/llamacpp-cpu/`).
`backend/tools/` está en `.gitignore`: no se versiona el binario.

### 2.2 Qué queda instalado

```
backend/tools/
├── llamacpp/            # GPU (la que usa Cuadra)
│   ├── llama-server.exe          # lanzador pequeño (~9 KB)
│   └── llama-server-impl.dll     # implementación real (aquí está todo)
└── llamacpp-cpu/        # respaldo sin GPU (si la VRAM está tomada)
```

### 2.3 Antivirus (Avast) — importante

Avast (Behavior Shield) **puso en cuarentena** `llama-server.exe` tras la primera
ejecución (veredicto `IDP.Generic`): el síntoma parece un cuelgue/CUDA OOM
(`ReadError WinError 10054`) y luego un `FileNotFoundError WinError 2`.

- **Solución aplicada**: exclusiones de Avast para `backend\tools` y `backend\models`.
- El instalador avisa si el `.exe` no aparece tras descomprimir.
- Si vuelve a pasar: revisa
  `C:\ProgramData\Avast Software\Avast\chest\index.xml` y restaura/exceptúa.

### 2.4 Requisito de PyAV

`faster-whisper` 1.2.x usa `av.open(..., metadata_errors="ignore")`, parámetro
**eliminado en av 19**. Por eso `requirements.txt` fija `av<19`:

```powershell
backend\.venv\Scripts\python.exe -m pip install "av<19"
```

## 3. Modelos GGUF

### 3.1 Dónde viven

Todos en `backend/models/` (en `.gitignore`; el binario no se versiona). Cuadra
elige modelo por, en orden: `CUADRA_LLM_MODEL_PATH` → `CUADRA_LLM_MODEL` (nombre en
`backend/models/`) → el primer `.gguf` que encuentre.

### 3.2 Traer un modelo nuevo

```powershell
# Del catálogo de LM Studio (copia UN gguf a backend\models):
cd backend
.\scripts\lab\fetch_model.ps1 -List
.\scripts\lab\fetch_model.ps1 -Fetch <clave>
```

O simplemente copia cualquier `.gguf` a `backend\models\`.

### 3.3 Modelo por defecto: `Bonsai-8B-Q1_0.gguf`

Elegido en el bake-off (ver `06_bakeoff_modelos.md`): 7/7 aciertos de intención,
~71 tok/s y ~2.36 GB VRAM a 8K de contexto, ~1.08 GB en disco.

| Modelo                    | Disco   | NLU  | tok/s | VRAM @8K |
| ------------------------- | ------- | ---- | ----- | -------- |
| **Bonsai-8B-Q1_0** (def.) | 1.08 GB | 7/7  | 71    | ~2.36 GB |
| Nemotron-3-Nano-4B-Q4     | 2.64 GB | 7/7  | 54    | ~2.60 GB |
| Qwen3-VL-4B-Q4            | 2.33 GB | 7/7  | 45    | ~2.96 GB |
| Qwen3.5-2B-Q6             | 1.45 GB | 7/7  | 84    | ~1.73 GB |
| Qwen3.5-2B-Q4             | 1.18 GB | 7/7  | 96    | ~1.45 GB |
| Bonsai-4B-Q1_0            | 0.53 GB | 5/7  | 100   | ~1.86 GB |

### 3.4 Cambiar de modelo en cualquier momento

No hace falta tocar código. En orden de comodidad:

```powershell
# 1) Por variable de entorno (aplica al próximo arranque / al reiniciar el server)
$env:CUADRA_LLM_MODEL = "Nemotron-3-Nano-4B-Q4_K_M.gguf"

# 2) Por CLI del supervisor (arranca/reinicia con otro modelo)
cd backend
.\scripts\start_llm.ps1 -Model Nemotron-3-Nano-4B-Q4_K_M.gguf
.\.venv\Scripts\python.exe -m cuadra.ai.llm_server restart --model <nombre>

# 3) Por API (el dashboard lo usará desde la interfaz)
POST /api/llm/restart   { "model": "Nemotron-3-Nano-4B-Q4_K_M.gguf" }
```

## 4. Servidor gestionado por Cuadra

Cuadra **arranca y para su propio `llama-server`** (`cuadra/ai/llm_server.py`):
al iniciar lo levanta en segundo plano (la API responde de inmediato) y al cerrar lo
detiene para **liberar la VRAM**; además mata huérfanos de sesiones previas por PID.

### 4.1 Flags con los que se lanza

```
-m <modelo.gguf> -c 8192 -ngl 99 -np 1 -rea off --jinja --no-webui
--host 127.0.0.1 --port 8080 [--api-key <clave>]
```

### 4.2 CLI y scripts

```powershell
cd backend
.\.venv\Scripts\python.exe -m cuadra.ai.llm_server status
.\.venv\Scripts\python.exe -m cuadra.ai.llm_server models
.\.venv\Scripts\python.exe -m cuadra.ai.llm_server start [--model NOMBRE]
.\.venv\Scripts\python.exe -m cuadra.ai.llm_server stop
.\.venv\Scripts\python.exe -m cuadra.ai.llm_server restart [--model NOMBRE]

.\scripts\start_llm.ps1 [-Model NOMBRE]   # atajos
.\scripts\stop_llm.ps1
```

### 4.3 Endpoints (PIN por `X-PIN`)

| Método | Ruta                 | Uso                                        |
| ------ | -------------------- | ------------------------------------------ |
| GET    | `/api/llm/status`    | proveedor, gestionado, corriendo, modelo   |
| GET    | `/api/llm/models`    | `.gguf` disponibles + modelo actual        |
| POST   | `/api/llm/start`     | arranca (`{"model": "..."}` opcional)      |
| POST   | `/api/llm/stop`      | detiene y libera VRAM                       |
| POST   | `/api/llm/restart`   | reinicia / cambia de modelo                |

`:8080/health`, `:8080/v1/models` y `:8080/v1/chat/completions` son del server.

### 4.4 Archivos de estado

- PID/info: `backend/data/llm_server.pid`
- Log del server: `backend/data/logs/llm.log`

### 4.5 Convivencia con LM Studio y Ollama

- **LM Studio** sirve API compatible con OpenAI en `127.0.0.1:1234`. Úsalo solo para
  **desarrollo/experimentos**; sostiene ~1 GB de VRAM al cargar. **Ciérralo** cuando
  uses Cuadra para no competir por la GPU.
- **Ollama no está instalado.** El cliente soporta el protocolo nativo (`CUADRA_LLM_PROVIDER=ollama`)
  por si algún día hiciera falta, pero **no es necesario** para Cuadra.

## 5. Whisper (STT) — sin instalar nada

- Modelo por defecto: `small` int8 en CPU (4 hilos). **Ya está descargado.**
- Caché de modelos: `%USERPROFILE%\.cache\huggingface\hub\`
  (`models--Systran--faster-whisper-small` y `...-base`).
- Cambiar de modelo: `$env:CUADRA_WHISPER_MODEL = "base" | "small" | "medium"`.
- Idioma: por defecto **autodetecta** (`CUADRA_WHISPER_LANGUAGE` vacío). En producción con
  clientes cubanos conviene fijar `$env:CUADRA_WHISPER_LANGUAGE = "es"`.

### 5.1 Si una descarga de modelo se queda colgada (nos pasó con `small`)

El backend `xet` de HuggingFace se quedó en 2.5 MB durante 15 minutos. Solución aplicada
(una sola vez; después queda en caché):

```powershell
$env:HF_HUB_DISABLE_XET = "1"
backend\.venv\Scripts\python.exe -c "from huggingface_hub import snapshot_download; print(snapshot_download('Systran/faster-whisper-small'))"
```

## 6. Higiene del prompt (lo que el LLM NO debe ver)

Lo que entra al contexto del modelo **puede repetirse al cliente**, así que se excluye
todo dato interno. `catalog.llm_product_json()` **no** envía `comision` ni
`moneda_comision` (el `Qwen3.5-2B-Q4` del bake-off llegó a filtrar "la comisión es de
$0.50"), y `prompts.py` prohíbe mencionar comisiones/márgenes/datos internos.

## 7. Carpetas de datos (se crean solas)

`config.ensure_dirs()` crea al arrancar (y `.gitignore` las excluye del repo):

```
backend/data/
├── cuadra.db            # SQLite (WAL)
├── fotos/product_{id}/img_001.jpg
├── audios/              # audio entrante / pruebas de debug
├── llm_server.pid       # PID del llama-server gestionado
└── logs/
    ├── cuadra.log       # RotatingFileHandler (5 MB x 5)
    └── llm.log          # salida del llama-server
```

## 8. Variables de entorno de Cuadra (resumen)

| Variable                    | Default                             | Uso                                    |
| --------------------------- | ----------------------------------- | -------------------------------------- |
| `CUADRA_LLM_PROVIDER`       | `openai`                            | `openai` \| `ollama`                   |
| `CUADRA_LLM_BASE_URL`       | `http://127.0.0.1:8080/v1`          | Endpoint del server (`.../v1` en openai) |
| `CUADRA_LLM_API_KEY`        | vacío                               | `--api-key` / `Authorization: Bearer`  |
| `CUADRA_LLM_MODEL`          | `Bonsai-8B-Q1_0.gguf`               | Modelo por defecto                     |
| `CUADRA_LLM_MODEL_PATH`     | vacío                               | Ruta explícita a un `.gguf`            |
| `CUADRA_LLM_MODELS_DIR`     | `backend/models`                    | Carpeta de modelos                     |
| `CUADRA_LLM_TIMEOUT_S`      | `120`                               | Timeout de una llamada al LLM          |
| `CUADRA_LLM_TEMPERATURE`    | `0.4`                               | Temperatura de redacción               |
| `CUADRA_LLM_MANAGE`         | `1`                                 | Cuadra gestiona su propio server       |
| `CUADRA_LLM_AUTOSTART`      | `1`                                 | Arrancarlo solo al iniciar             |
| `CUADRA_LLM_BIN`            | `backend/tools/llamacpp/llama-server.exe` | Binario del server               |
| `CUADRA_LLM_HOST` / `_PORT` | `127.0.0.1` / `8080`                | Host/puerto del server                 |
| `CUADRA_LLM_GPU_LAYERS`     | `99`                                | Capas en GPU (`-ngl`)                  |
| `CUADRA_LLM_CTX`            | `8192`                              | Ventana de contexto (`-c`)             |
| `CUADRA_LLM_EXTRA_ARGS`     | vacío                               | Flags extra de `llama-server`          |
| `CUADRA_WHISPER_MODEL`      | `small`                             | STT                                    |
| `CUADRA_WHISPER_LANGUAGE`   | vacío (auto)                        | `es` para fijar español                |
| `CUADRA_WHISPER_THREADS`    | `4`                                 | Hilos CPU de Whisper                   |
| `CUADRA_PIN`                | `0000`                              | PIN del dashboard                      |
| `CUADRA_DEBUG`              | `1`                                 | Habilita `/api/debug/*`                |
| `CUADRA_PORT`               | `8321`                              | Puerto de la API                       |

## 9. Verificación final (5 minutos)

```powershell
# 1) Runtime presente (si falta, re-instala o revisa el antivirus, §2.3)
cd backend
Test-Path .\tools\llamacpp\llama-server.exe        # True

# 2) Arranca el core (arranca el modelo solo; la API responde al instante)
.\.venv\Scripts\python.exe -m uvicorn cuadra.main:app --host 127.0.0.1 --port 8321

# 3) En otra consola: estado del LLM y salud
curl -H "X-PIN: 0000" http://127.0.0.1:8321/api/llm/status   # "running": true
curl -H "X-PIN: 0000" http://127.0.0.1:8321/api/health       # "llm": { "status": "ok", ... }

# 4) Simula un turno completo (requiere al menos 1 producto en el catálogo)
curl -X POST http://127.0.0.1:8321/api/debug/message -H "X-PIN: 0000" `
     -H "Content-Type: application/json" `
     -d '{\"text\": \"hola, cuanto esta el cable?\", \"identifier\": \"prueba\"}'
```

> Si `running` sale `false`, mira `backend/data/logs/llm.log`. Si el mensaje de prueba
> responde con el precio real del catálogo (y sin datos internos), el LLM está verificado
> de punta a punta.

## 10. Experimentos pendientes

Documentados en `06_bakeoff_modelos.md` (latencia como naturalidad, Bonsai-8B a 16K vs
Nemotron-4B, tool/function-calling por modelo, modelos Bonsai sin probar, etc.). El código
del banco de pruebas vive aislado en `backend/scripts/lab/`.
