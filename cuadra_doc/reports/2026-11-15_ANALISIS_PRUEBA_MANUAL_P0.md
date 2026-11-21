# Analisis de prueba manual — Test P0 con Proveedor Tres (TCP unico)

**Fecha:** 2026-11-15, 17:52–17:58 UTC
**Canal:** Telegram userbot (`@repdcuadra`)
**Base de datos:** `backend/data/cuadra.db` (sembrada con `seed_single_supplier.py`)
**Resultado:** 0 ventas creadas, cliente se fue frustrado

---

## 1. Resumen ejecutivo

La prueba manual con los cambios P0 fue **desastrosa**. El cliente escribió 11 mensajes y el bot respondió 11 veces, pero **no se creó ninguna venta**. El cliente terminó diciendo "Ya te dije lo que quería" (m21) y el bot respondió con la misma plantilla genérica (m22).

| Métrica | Valor |
|---|---|
| Conversaciones | 1 |
| Mensajes | 22 (11 cliente, 11 bot) |
| Ventas creadas | 0 |
| Escalaciones | 0 |
| Latencia p50 | ~3,0–3,9 s |
| Errores LLM | 0 |

El sistema no falló técnicamente, pero **no retuvo ningún dato entre turnos**, tratando cada mensaje como si fuera el primero.

---

## 2. Timeline completo de la conversación

| Msg | Hora | Autor | Contenido | Respuesta del bot | Problema |
|-----|------|-------|-----------|-------------------|----------|
| m1 | 17:52:12 | CLI | "Hola" | m3: "¿Dime cuál producto te interesa..." | — |
| m2 | 17:52:16 | CLI | "Buenas tarde3s" | m4: **"¿Dime cuál producto te interesa..."** | C1: Repetición idéntica a m3 |
| m5 | 17:53:06 | CLI | "Tienes laptops" | m6: **"¿Dime cuál producto te interesa..."** | C1: No responde sobre laptops, repite plantilla |
| m7 | 17:53:37 | CLI | "Me interesa comprar una laptop, que tienes disponible" | m8: "Para dejarte el pedido listo me falta la zona..." | C3: Salta a captura sin mostrar catálogo |
| m9 | 17:55:16 | CLI | "Calle Prueba 110..., 55500000" | m10: **"Para dejarte el pedido listo me falta la zona..."** | C2: Ya dio dirección+teléfono, ignorados |
| m11 | 17:55:31 | CLI | "Playa" | m12: **"¿Dime cuál producto te interesa..."** | C2: Ya dio zona, bot resetea |
| m13 | 17:56:10 | CLI | "Me interesa comprar tres laptops, que tipo de laptop tienes..." | m14: **"¿Dime cuál producto te interesa..."** | C2+C3: No responde tipos, resetea |
| m15 | 17:56:37 | CLI | "Laptop Ultraligera" | m16: "Sí, tenemos Laptop Ultraligera. Está en 720 USD..." | Primera vez que da precio |
| m17 | 17:57:01 | CLI | "quiero 3" | m18: "Para dejarte el pedido listo me falta la dirección..." | C4: No interpreta cantidad, pide dirección ya dada |
| m19 | 17:57:22 | CLI | "Calle Prueba 110..., 55500000" | m20: **"¿Dime cuál producto te interesa..."** | C2: Repite dirección, bot la ignora |
| m21 | 17:57:53 | CLI | "Ya te dije lo que quería" | m22: **"¿Dime cuál producto te interesa..."** | C5: No maneja enfado |

---

## 3. Causas raíz identificadas

### C1. Repetición de plantilla idéntica
m3=m4, m12=m14=m20=m22. El cliente recibe "¿Dime cuál producto te interesa?" 5 veces seguidas.

**Por qué pasa:** `template_variant()` usa `context[f"last_template_{name}"]` para recordar la última variante, pero el contexto no se persiste correctamente entre turnos, o `_variant_index` siempre devuelve 0 porque `last` es None (el contexto se reinicia).

### C2. Pérdida total de contexto entre turnos
El cliente da:
- Dirección + teléfono (m9) → ignorado en m10
- Zona "Playa" (m11) → ignorado en m12
- Dirección + teléfono otra vez (m19) → ignorado en m20

Cada vez, el bot responde con `ask_which_product` como si no supiera nada.

**Por qué pasa:** `capture_facts` escribe los hechos en `conversation_facts`, pero `_sale_slots` lee `facts_map` que debería recuperarlos. El problema está en que entre turnos el contexto de `stage` se pierde, o `_resolve_product` no encuentra el producto y el flujo entra en `ask_which_product` en lugar de continuar la captura.

### C3. Salta directo a capturar datos sin mostrar catálogo
m7 "Me interesa comprar una laptop, que tienes disponible" → m8 pide zona directamente, sin mostrar qué laptops hay ni precios.

**Por qué pasa:** `_maybe_handle_sale` con `intent.intencion == "comprar"` y `stage` inicial va directo a `_missing_slots` y responde `sale_ask_data`, sin nunca mostrar el catálogo de productos disponibles.

### C4. No interpreta cantidad "quiero 3"
m17 "quiero 3" → m18 pide dirección otra vez.

**Por qué pasa:** El NLU ligero extrae `cantidad`, pero `_maybe_handle_sale` no la usa correctamente cuando ya hay un `stage` de `CAPTURE_DATA` abierto. El flujo pide datos de entrega en lugar de usar la cantidad.

### C5. No maneja enfado del cliente
m21 "Ya te dije lo que quería" → m22 repite plantilla genérica.

**Por qué pasa:** No hay detección de frustración ni ruta de rescate.

---

## 4. P1 en el tientero (borrador, sin implementar)

Los cambios de P1 que ya teníamos planeados, **pendientes de ajustar tras este análisis**:

| # | Cambio propuesto | Archivo | Estado |
|---|-----------------|---------|--------|
| 1 | Persistir datos capturados en `context` entre turnos (zona, dirección, teléfono, nombre, cantidad) para no repetir preguntas | `ai/pipeline.py` | **En tientero** — prioridad máxima tras este análisis |
| 2 | Ruta de queja/frustración cuando el cliente repite la misma petición 3+ veces | `ai/pipeline.py` | **En tientero** — necesario por C5 |
| 3 | No resetear contexto en medio de una compra (saludo no debe reiniciar el flujo) | `ai/pipeline.py` | **En tientero** — necesario por C2 |
| 4 | Validar zona activa antes de escalar (UCI no debería escalar al TCP) | `ai/pipeline.py` + `domain/catalog.py` | **En tientero** |
| 5 | Mostrar catálogo de productos antes de pedir datos de entrega | `ai/pipeline.py` | **En tientero** — necesario por C3 |
| 6 | Usar `intent.cantidad` cuando hay venta abierta en `CAPTURE_DATA` | `ai/pipeline.py` | **En tientero** — necesario por C4 |

**Decisión abierta:** ¿Qué orden de P1 implementamos primero, dado que C2 (contexto) es la causa raíz de C3 y C4?

---

## 5. Próximo paso sugerido

Antes de tocar código en P1, necesitamos entender **por qué el contexto no se persiste**. Revisar:
1. Si `facts_map` devuelve los datos guardados por `capture_facts`
2. Si `_resolve_product` encuentra el producto cuando el contexto tiene `product_id`
3. Si `template_variant` realmente rota variantes o siempre devuelve la primera

¿Quieres que investigemos la causa raíz de C2 (contexto no persistente) antes de seguir con P1?
