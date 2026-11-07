---
tipo: cierre_fase
proyecto: Cuadra
modulo: agente_ia
estado: FASE 2 — implementada, pendiente medición con consultas reales
fecha: 2026-11-06
tags: [fase-2, memoria, hechos, resumen, fts5, embeddings, retrieval, zonas]
---

# 08 — Cierre de la Fase 2

> Memoria por capas, hechos fijados, resumen incremental por id de mensaje,
> retrieval híbrido con degradación segura y cierre de conversaciones inactivas.
> **Sin tocar la base real ni la operación**: todo se probó sobre bases temporales
> y el modelo de embeddings no se descarga al arrancar.

---

## 1. Qué cambió

| Archivo | Cambio |
|---|---|
| `migrations/013_memory_retrieval.sql` | `conversation_facts`, `products_fts` + backfill + triggers, `product_embeddings`, `zone_aliases`, `conversations.closed_at`. |
| `ai/memory.py` (nuevo) | Hechos vigentes, perfil de cliente y resumen incremental por `summary_through_message_id`. |
| `ai/retrieval.py` (nuevo) | FTS5 BM25 + FastEmbed opcional + RRF (`k=60`) + fallback fuzzy. |
| `scripts/fetch_embeddings_model.py` (nuevo) | Descarga única del modelo multilingüe a `backend/models/fastembed/`. |
| `ai/handlers.py` | Jobs `reindex_product` / `reindex_all_products`; reindexado tras respuestas TCP. |
| `ai/pipeline.py` | Hechos deterministas por turno; retrieval híbrido al resolver productos y comparar. |
| `ai/context.py` | Inyecta hechos y perfil; nunca se recortan del presupuesto. |
| `domain/catalog.py` | `resolve_zone`, CRUD básico de alias y `zonas_entrega` como lista. |
| `domain/conversations.py` | `close_idle(hours)` y `closed_at`. |
| `scheduler/maintenance.py` | Job diario `close_idle_conversations`. |
| `api/routes_catalog.py` | Encola `reindex_product` al crear, editar o borrar un producto. |
| `main.py` | Reindexado inicial de baja prioridad solo si el modelo está vendorizado. |
| `requirements.txt` | `fastembed` añadido; el camino semántico sigue siendo opcional. |

Configuración nueva:

- `CUADRA_CONVERSATION_IDLE_HOURS` (72 por defecto).
- `CUADRA_FASTEMBED_DIR` (`backend/models/fastembed/`).
- `CUADRA_EMBEDDING_MODEL`, `CUADRA_EMBEDDING_THREADS`.
- `CUADRA_RETRIEVAL_CANDIDATES` (20), `CUADRA_RETRIEVAL_TOP_K` (4), `CUADRA_RRF_K` (60).
- `PRIORITY_MAINTENANCE = 200` (el reindexado nunca compite con un cliente).

---

## 2. Memoria por capas

| Capa | Dónde vive | Comportamiento |
|---|---|---|
| 4. Perfil | `customers.preferences_json` + `sales` | Nombre, zona habitual, teléfono, compras anteriores. |
| 3. Resumen | `conversations.context_json.summary` | Avanza por `summary_through_message_id`; nunca por conteo. |
| 2. Hechos | `conversation_facts` | Valor vigente por clave; `ON CONFLICT` actualiza, no crece. |
| 1. Buffer | `messages` | Últimos `HISTORY_WINDOW` mensajes exactos. |

Hechos extraídos sin llamada extra al modelo: producto en juego, zona, presupuesto,
nombre, teléfono y objeción. Las reglas de extracción están en Python y son
deliberadamente conservadoras; no sustituyen los slots de la Fase 4.

Reglas del resumen:

- Solo se resume el delta no cubierto por `summary_through_message_id`.
- Los últimos `HISTORY_WINDOW` mensajes se quedan textuales.
- No se resume mientras exista un turno `PROCESSING`.
- Si el modelo falla, se conserva el resumen anterior y se reintenta al siguiente turno.

Hechos y perfil **nunca** se recortan: si el presupuesto aprieta, primero se reduce el
historial, luego la lista de candidatos y por último el texto del turno.

---

## 3. Retrieval híbrido

```text
consulta
  ├─ FTS5 top 20 (BM25, nombre con más peso)
  └─ embeddings top 20 (coseno, si el modelo existe)
        └─ Reciprocal Rank Fusion (k=60)
              └─ top 4 productos reales, en el orden fusionado
                    └─ ACTIVE y OUT_OF_STOCK (nunca PAUSED ni REMOVED)
```

- **Léxico**: `products_fts` indexa `name`, `description`, nombre de categoría y
  `specifications_json`. El tokenizer `unicode61 remove_diacritics 2` hace que
  “batería” encuentre “bateria”. El backfill y los triggers se crean en la migración.
- **Semántico**: `paraphrase-multilingual-MiniLM-L12-v2` en CPU, vendorizado. Si falta
  `fastembed` o el `.onnx`, el sistema arranca igual y usa FTS5 + fuzzy.
- **Fusión**: RRF suma `1 / (k + rank)` de ambas listas; el orden se preserva al cargar
  de SQLite.
- **Fallback**: si ambas capas no devuelven nada, se usa `catalog.search_products`.
- **Zonas**: las zonas del producto salen de `supplier_delivery_zones` como lista
  (`zonas_entrega`) y se conserva `zona_entrega` como texto para las plantillas.
  `resolve_zone` valida contra `zones` activas + `zone_aliases`; no hay municipios
  hardcodeados y `San Miguel` no se renombra.
- **Borrado de zonas**: `DELETE /api/zones/{id}` ya no elimina la fila; la desactiva
  (`active = 0`) y conserva alias y vínculos con grupos, en línea con D3. El dashboard
  muestra la acción como “Desactivar zona”.

---

## 4. Cierre por inactividad

- El scheduler corre el cierre al arrancar (2 minutos después) y luego cada 24 h.
- Cierra conversaciones sin mensajes durante `CUADRA_CONVERSATION_IDLE_HOURS` (72 h):
  `status='CLOSED'`, `closed_at=now`, y cancela turnos `OPEN` viejos.
- Al volver, `get_or_create_open_conversation` crea una conversación nueva; el perfil
  y las compras anteriores siguen disponibles.

---

## 5. Aceptación

| Criterio (doc 04, Fase 2) | Estado |
|---|---|
| Tras 50 mensajes se conservan producto, zona y presupuesto | ✅ `test_facts_survive_fifty_messages_and_a_summary`. |
| Recall@3 ≥ 95 % en 50 consultas reales | ⏳ Pendiente: requiere consultas reales del dueño; no se inventa la cifra. |
| FTS5 < 10 ms con 1,000 productos simulados | ✅ Medido: **3.17 ms/consulta** (200 consultas, 20 resultados, base temporal). |
| Sin modelo de embeddings el bot sigue respondiendo por fuzzy | ✅ `test_fuzzy_fallback_when_both_layers_return_nothing` + `test_index_without_model_is_a_noop`. |
| `zones` + `zone_aliases` resuelven casos reales; `San Miguel` no se renombra | ✅ `ZoneTests`: nombre largo, alias normalizado, zona inactiva y zona desconocida. |

Pruebas: **61 casos** en `unittest` (24 de Fase 0 + 19 de Fase 1 + 18 de Fase 2),
todos en verde.

```powershell
cd backend
.venv\Scripts\python.exe -m unittest discover -s tests -q
```

---

## 6. Limitaciones de esta fase

- **Embeddings instalados**: `fastembed 0.9.1` está en `backend/.venv` y el modelo
  `paraphrase-multilingual-MiniLM-L12-v2` está vendorizado en
  `backend/models/fastembed/` con SHA-256 verificado. La carga offline y la inferencia
  (384 dimensiones) están comprobadas.
- **Descarga sin reanudación**: `huggingface_hub 1.33` reinicia el archivo completo si
  se corta, y en esta red `curl` está bloqueado. Para redes lentas hay que completar
  `model_optimized.onnx` con `Range` y verificar su SHA-256 antes de dar el modelo por
  bueno. El arranque nunca descarga.
- **Recall sin medir**: no hay 50 consultas reales etiquetadas todavía; solo pruebas
  sintéticas de FTS/fuzzy/RRF.
- **Extracción de hechos determinista**: zona, presupuesto, nombre y teléfono se detectan
  por patrones. Los slots completos y la validación fina llegan en la Fase 4.
- **Embeddings y categorías**: renombrar una categoría no reindexa automáticamente el
  campo `category` de FTS; un cambio de producto sí lo hace por trigger.
- **Alias sin UI**: el dominio ya permite crear/borrar alias, pero el dashboard todavía
  no tiene pantalla para administrarlos.
- **Sin commit**: la Fase 2 queda en el árbol de trabajo a la espera de revisión.

---

## 7. Próximo paso

1. Revisar este diff con el dueño; **no se hace commit sin pedido explícito**.
2. Medir recall@3 con 50 consultas reales etiquetadas usando el modelo ya vendorizado.
   `scripts/fetch_embeddings_model.py` solo hace falta si hay que reinstalar el modelo.
3. Pasar a la Fase 3 (ventas y finanzas), manteniendo `sales.mode` y los settings de
   autonomía como interruptores de comportamiento.
