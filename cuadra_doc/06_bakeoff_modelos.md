# 06 · Evaluación de modelos locales (bake-off)

Este documento registra la prueba comparativa de modelos locales para el motor de
lenguaje de Cuadra, el modelo elegido por defecto y las decisiones/experimentos
pendientes. El código de estas pruebas vive aparte, en `backend/scripts/lab/`
(ver su `README.md`), para no mezclarlo con la aplicación.

## Objetivo

Elegir el modelo GGUF que Cuadra usará para entender al cliente (clasificar
intención) y redactar respuestas, con estas prioridades:

1. **Capacidad**: vendedor, uso de herramientas (a futuro), manejar el volumen de
   contexto (resumen + últimos mensajes + datos de producto).
2. **Velocidad**: que quepa **completo en la VRAM** con una ventana de contexto
   razonablemente grande.
3. **Español** decente (tono cubano educado).
4. **Visión** solo si aporta de verdad (el modelo de visión probado salió débil).

## Entorno de prueba

- GPU: **NVIDIA GeForce RTX 3050 Laptop, 4095 MiB**, driver 610.47.
- Motor: **llama.cpp propio** (`backend/tools/llamacpp`), build **b11739**
  (0.7.0-dev, CUDA 13.4). Runtime instalado con `backend/scripts/install_llm.ps1`.
- Cómo se lanzó el servidor en cada prueba:
  `llama-server-impl` vía `llama-server.exe` con
  `-c <ctx> -ngl 99 -np 1 -rea off --jinja --no-webui`
  (`-rea off` = sin razonamiento extendido, para latencia estable).
- Esquema JSON de salida de la intención forzado con `response_format` +
  `--jinja`: **7/7 modelos devolvieron JSON válido y conforme al esquema**.

## Metodología

Batería con el **prompt real** de Cuadra (`cuadra/ai/prompts.py`) y los productos
semilla reales (Cargador 20W USB-C, Cable USB-C 1m, Power Bank 10000mAh agotado):

- **7 casos de NLU** (clasificación): precio, precio con errores de escritura,
  stock, compra, dato faltante (garantía), saludo, comparación.
- **2 casos de redacción** (compose): comparación y queja.
- Métricas: JSON válido, esquema, intención correcta, tokens/s, latencia, VRAM.

## Resultados

VRAM = memoria de GPU **usada por el modelo + caché KV** (medida con `nvidia-smi`;
si LM Studio estaba abierto, se descuenta su consumo).

| Modelo | Disco | NLU (7) | tok/s | Carga | VRAM @8k | Comentario |
|---|---|---|---|---|---|---|
| **Bonsai-8B-Q1_0** | 1.08 GB | **7/7** | 71 | 2.3 s | ~2.36 GB | **Elegido.** Español correcto, VRAM holgada (aguanta 16k). |
| Nemotron-3-Nano-4B-Q4_K_M | 2.64 GB | 7/7 | 54 | 2.2 s | ~2.60 GB | Mejor español, pero VRAM al límite (~390 MB libres @8k). |
| Qwen3-VL-4B-Instruct-Q4_K_M | 2.33 GB | 7/7 | 45 | 5.3 s | ~2.96 GB | Mejor redacción; el más lento; inventó "24-48 horas". |
| Qwen3.5-2B-Q6_K | 1.45 GB | 7/7 | 84 | — | ~1.73 GB | Rápido; alucinó ("ya te envié el catálogo"). |
| Qwen3.5-2B-Q4_K_M | 1.18 GB | 7/7 | 96 | 2.2 s | ~1.45 GB | El más rápido y liviano; **filtró la comisión interna**. |
| Bonsai-4B-Q1_0 | 0.53 GB | 5/7 | 100 | 1.2 s | ~1.86 GB | Muy rápido pero confundió 2 intenciones. |

Descartados: **Bonsai-27B** (no cabe en la VRAM), **moondream2** (solo visión, sin
chat), **gemma-4-E4B** (5.34 GB, no cabe), **Wan2.2** (video), **nomic-embed**
(embeddings, reservado para RAG futuro).

### Contexto vs VRAM y velocidad

Bonsai-8B-Q1_0 a mayor contexto (KV crece ~1.1 GB por pasar de 8k a 16k):

| Contexto | tok/s | VRAM usada |
|---|---|---|
| 8192 | 71 | ~2.36 GB |
| 16384 | 63 | ~3.48 GB |

Con LM Studio cerrado, Bonsai-8B entra completo incluso a 16k.

## Hallazgos

1. **Antivirus borra el runtime (Avast).** Avast marcó los binarios de llama.cpp
   como `IDP.Generic` (falso positivo de comportamiento) y **los puso en cuarentena
   después de la primera ejecución**, matando el proceso a mitad de inferencia.
   Síntoma engañoso: parecía un fallo de VRAM/OOM. Solución: exclusión en Avast para
   `backend\tools` y `backend\models`. `install_llm.ps1` ahora avisa si faltan los `.exe`.
2. **Fuga de datos internos al cliente (bug).** `catalog.llm_product_json()` inyecta
   `comision`/`moneda_comision` en el contexto del LLM y el prompt no lo prohíbe; el
   modelo de 2B lo repitió al cliente y sumó mal el total. **A corregir:** enviar al
   LLM una vista segura del producto (sin comisión ni datos de proveedor) **y** una
   regla explícita en el prompt. Regla general: *todo lo que se pone en el contexto
   puede acabar en la boca del cliente*.
3. **Alucinaciones menores en modelos pequeños**: 2B inventó acciones pasadas;
   Qwen3-VL-4B inventó un plazo de entrega. Refuerza la necesidad de plantillas
   cerradas (precio/stock) y de prohibir datos no presentes en el contexto.
4. **El esquema JSON funciona** en todos los modelos gracias a `--jinja`: no hace
   falta reparar la salida.
5. Bonsai-4B confundió stock→precio y saludo→queja: **demasiado pequeño** para el NLU fino.

## Decisión

- **Modelo por defecto:** `Bonsai-8B-Q1_0.gguf`, con **contexto 8192** por defecto.
- **Seleccionable en cualquier momento:** Cuadra no fija el modelo en el código;
  se configura con `LLM_MODEL` / `LLM_MODEL_PATH`. A futuro, el **panel web** permitirá
  elegir entre los GGUF presentes en `backend/models/` y reiniciar el servidor local.
- Cambiar de modelo **no** requiere tocar código (ni el laboratorio).

## Experimentos futuros (ya anotados)

- **Contexto dentro de la VRAM**: comparar Bonsai-8B a **16k** contra Nemotron-4B
  con **menos contexto**, o con contexto limitado generando menos tokens/s.
- **Latencia como naturalidad**: como el bot tiene una ventana de tiempo para
  responder, un procesamiento algo más lento **puede sonar más humano**. Medir
  "naturalidad percibida" contra el presupuesto de tiempo de respuesta.
- **Uso de herramientas (tool/function calling)**: validar qué modelos lo hacen bien
  antes de diseñar features que dependan de ello.
- **Cerrar LM Studio en operación** para liberar ~1 GB de VRAM.
- Probar los no evaluados: **Bonsai-1.7B** y **Ternary-Bonsai-4B-Q2**.

## Reproducir las pruebas

```powershell
# desde backend/
powershell -File scripts\lab\fetch_model.ps1 -List
powershell -File scripts\lab\fetch_model.ps1 -Fetch bonsai-8b
.\.venv\Scripts\python.exe scripts\lab\bakeoff.py --model models\Bonsai-8B-Q1_0.gguf --ctx 8192
```

Detalle crudo de cada corrida en `backend/data/logs/bakeoff_<modelo>.json`.
