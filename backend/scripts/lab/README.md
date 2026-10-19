# Laboratorio de modelos (código de pruebas)

Esta carpeta es el **banco de pruebas** de Cuadra: aquí vive el código que usamos
para *probar* modelos de lenguaje, separado a propósito del código real de la
aplicación. Si algo se rompe aquí, la app no se ve afectada.

> El código real de Cuadra vive en `backend/cuadra/`. **No** toques nada de aquí
> para el funcionamiento normal; esto es para experimentar.

## ¿Qué hay aquí?

| Archivo | Para qué sirve |
|---|---|
| `bakeoff.py` | Arranca un modelo, le corre una batería de pruebas (intención + redacción) y saca métricas: aciertos, velocidad (tok/s), latencia y VRAM. |
| `fetch_model.ps1` | Copia **un** modelo a la vez desde el almacén de LM Studio hacia `backend/models/`. También lista candidatos y limpia. |

## Uso rápido (PowerShell)

Todos los comandos se ejecutan **desde `backend/`**.

Listar modelos candidatos y ver cuál está copiado ahora:
```powershell
powershell -File scripts\lab\fetch_model.ps1 -List
```

Copiar un candidato (borra el que hubiera para no acumular disco):
```powershell
powershell -File scripts\lab\fetch_model.ps1 -Fetch bonsai-8b
```

Correr las pruebas sobre el modelo copiado:
```powershell
.\.venv\Scripts\python.exe scripts\lab\bakeoff.py --model models\Bonsai-8B-Q1_0.gguf
```

Opciones útiles de `bakeoff.py`:
- `--ctx 16384` → ventana de contexto (por defecto 8192).
- `--port 8080` → puerto del servidor local.
- `--no-server` → reutiliza un servidor ya encendido.
- `--ngl 99` → capas en GPU (99 = todas).

Borrar todos los `.gguf` copiados:
```powershell
powershell -File scripts\lab\fetch_model.ps1 -Clean
```

## Qué mide el bake-off

- **NLU** (clasificación de intención): 7 mensajes con el *mismo prompt real* de
  Cuadra (`cuadra/ai/prompts.py`). Cuenta JSON válido, JSON conforme al esquema e
  intención correcta.
- **Redacción (compose)**: 2 respuestas libres para revisar calidad del español.
- **Velocidad**: tokens/segundo y latencia por respuesta.
- **VRAM**: memoria de GPU usada (con `nvidia-smi`).
- Guarda el detalle en `backend/data/logs/bakeoff_<modelo>.json`.

## Advertencias

- **Antivirus:** algunos (p. ej. Avast) marcan los binarios de `llama.cpp` como
  `IDP.Generic` y los borran *después* de ejecutarse una vez. Si el servidor
  "desaparece", agrega una exclusión para `backend\tools` y `backend\models`, y
  re-ejecuta `scripts\install_llm.ps1`.
- **Un modelo a la vez:** la GPU (RTX 3050, 4 GB) no aguanta dos modelos cargados.
- **Cierra LM Studio** si está usando la GPU, para dejar más VRAM libre.

## El código real

Para cambiar el modelo que usa Cuadra en producción **no** se toca esta carpeta:
se usa la configuración (`LLM_MODEL` / `LLM_MODEL_PATH`) y, a futuro, el selector
de modelo del panel web.
