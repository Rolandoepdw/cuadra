# Análisis de prueba #5 — ficha, listado y cobertura

**Fecha:** 2026-11-17
**Entorno:** servicio en marcha, BD real, trazado en `ALL`.
**Alcance:** conversación repetida de la prueba #4 (cliente @cliente_demo, TCP @proveedor_tres), con foco en detalles de producto, listado en tabla y zonas.

---

## 1. Hallazgos

| # | Síntoma | Causa | Corrección |
|---|---|---|---|
| F17 | «¿Qué propiedades tiene la ultraligera?» respondió «¿Qué dato necesitas?» | El guardrail de specs solo miraba `specifications_json`; la ficha real está en `description` (`ASUS ZenBook / Ryzen 7 / 16GB RAM / 1TB SSD`) | `_product_spec_corpus` y `_specs_corpus` incluyen descripción + nombre; `SPEC_VALUE_HINTS` reconoce valores sin etiqueta |
| F18 | Turnos de detalles de 6,5–7,8 s | `_compose_open` + reintento + fallback | `_decide` responde determinísticamente desde la ficha (`_render_details`); solo compone lo que ninguna plantilla cubre |
| F19 | «Qué otras propiedades tiene» cayó en alternativas | `_ALTERNATIVES` incluía `otro/otra/otros/otras` sueltos | Se eliminaron; `_OPEN_DETAILS` detecta la petición de ficha |
| F20 | «¿Qué laptops tienes?» cotizaba un producto | No había listado de categoría | `catalog_table` + `_format_product_table` (ID, producto, precio, características; máx. 8 filas) |
| F21 | Una categoría desnuda podía iniciar una venta | `_maybe_handle_sale` aceptaba `comprar` con categoría | `_bare_category` devuelve `None` y `_decide` lista la tabla |
| F22 | «La UCI» sola no resolvía | Los nombres cortos exigían pista | `_zone_key` compara sin artículos ni palabras de entrega |
| F23 | «Marianao o La Lisa» se escalaba o se ignoraba | `resolve_zone` devolvía `None` con varias zonas | `resolve_zones` + `zone_partial_coverage` responde por cobertura real |
| F24 | «¿Puedes entregar en mi zona?» se escaló al TCP | No existía ruta de cobertura sin zona | `_delivery_zones_outcome` lista las zonas del proveedor |
| F25 | «no recuerdo, consulta en la base de datos» quedó como ficha y se reenvió | `_NOT_AN_ANSWER` no cubría la derivación a la base de datos | Se amplió; la escalación queda `PENDING`, evento `escalation_unconfirmed`, sin respuesta al cliente |
| F26 | Respuestas del TCP guardadas bajo `dato_proveedor` | El fallback usaba una clave cajón | `spec_topic(question, answer)` guarda bajo `procesador`, `garantía`, etc. |
| F27 | El contexto podía quedarse corto | `HISTORY_WINDOW=12` (~6 turnos) | `HISTORY_WINDOW=20` y `LLM_MAX_PROMPT_TOKENS=3200`; la traza registra `history_messages` |

---

## 2. Comportamiento nuevo

### Ficha abierta

```
Cliente: ID 2, ¿qué propiedades tiene?
Bot: Esto es lo que tengo de Laptop Ultraligera: ASUS ZenBook · Ryzen 7 ·
     16GB RAM · 1TB SSD. Si quieres, te confirmo algún dato puntual con el almacén.
```

Sin llamada al compositor y sin escalación.

### Dato concreto que falta

```
Cliente: ID 2, ¿qué garantía trae?
Bot: Esto es lo que tengo de Laptop Ultraligera: ASUS ZenBook · Ryzen 7 ·
     16GB RAM · 1TB SSD. No tengo el dato de garantía; consulto al almacén y
     te confirmo. ¿Te interesa que te reserve uno?
```

Se responde lo que hay y se escala solo `garantía` al TCP.

### Listado en tabla

````
Esto es lo que tengo en laptops:
```
ID  Producto              Precio    Características
--  --------------------  --------  ------------------------------
1   Laptop Empresarial    350 USD   16GB RAM, 512GB SSD, Lenovo T…
2   Laptop Ultraligera    720 USD   agotado, Ryzen 7, 16GB RAM, 1…
13  Laptop Gamer RTX 40…  1100 USD  última unidad, RTX 4060, 16GB…
```
¿Cuál te interesa? Dime el ID o el nombre.
````

La columna de características prioriza disponibilidad y specs; el nombre se
recorta si no cabe. El bloque ``` se envía con `parse_mode="Markdown"` solo en
los mensajes que lo contienen, con reintento en texto plano si Telegram rechaza
las entidades.

### Zonas

- «La UCI» resuelve sin pista (zona inactiva → `zone_not_covered`).
- «Marianao o La Lisa» → «Sí entregamos en Marianao, pero en La Lisa no llegamos
  por ahora…» (sin escalación).
- «¿Puedes entregar en mi zona?» → lista las zonas reales del proveedor.

---

## 3. Datos corregidos

- Producto 2 (`Laptop Ultraligera`): se eliminó de `specifications_json` el valor
  `dato_proveedor = "no recuerdo, consulta en la base de datos, q seguro hay
  varios de ellos"`. La descripción real (`ASUS ZenBook / Ryzen 7 / 16GB RAM /
  1TB SSD`) sigue siendo la fuente de la ficha.

---

## 4. Pruebas

- Suite completa: **381 tests OK**.
- Nuevos: `PruebaCincoTests` (12) — ficha abierta/concreta/faltante, guardrail
  con descripción, tabla de categoría, categoría desnuda sin venta, fence solo
  con `table=True`, zonas cortas, multi-zona, entrega sin zona y entrega en zona
  cubierta.
- TCP: no-respuesta mantiene `PENDING`, sin salida al cliente; fallback guarda
  bajo `garantía`.

---

## 5. Pendientes

- `SPEC_TOPICS`/miles de productos: fuera de alcance de esta prueba.
- Validar en Telegram real el render de la tabla (Markdown del bot y userbot).
- Revisar si el listado debe paginar cuando el catálogo crezca (> 8 por
  categoría).
