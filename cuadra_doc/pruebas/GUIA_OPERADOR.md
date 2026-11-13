# Guía del operador — test real de Cuadra

Para ti. Prepara la sesión, vigila el sistema mientras tus amigos hablan y
convierte los diarios en números. Los documentos de cliente y TCP son
`GUIA_CLIENTE.md` y `GUIA_TCP.md`, en esta misma carpeta.

## 1. Estado de partida (verificado 2026-11-12, 21:42–21:59)

| Comprobación | Resultado |
|---|---|
| Core | En línea, 18 migraciones aplicadas, integridad `ok` |
| TCP | 4 (Claudia, Roger, Rolando, Yoan), todos con `telegram_peer_id` |
| Catálogo | 41 productos: Claudia 7, Roger 15, Rolando 14, Yoan 5 |
| Difusiones | 34 campañas (33 activas, 1 desactivada), **37 posts enviados, 0 fallos** |
| Estrategia | `repeat_passes = 1` verificado: ninguna campaña publicó dos veces en el mismo grupo |
| LLM | **Caído** al escribir esto (`Salud` → `unreachable`). Arráncalo antes de la sesión |
| Modo de ventas | **Asistido**: cada venta queda «Por aprobar» hasta que la apruebes |

### Lo que no se publicó (a propósito)

| Campaña | Motivo |
|---|---|
| Cable HDMI 2 m | Categoría `accesorios` sin grupo con esa categoría |
| Combo Personalizado | Producto sin categoría y ningún grupo libre activo |
| Moto Eléctrica Delivery | Solo apunta a un grupo pausado |

No las persigas: son casos del seed para probar los filtros. El resto del
catálogo sí está publicado.

## 2. Antes de la sesión

| ✓ | Acción | Dónde | Qué debe pasar |
|---|---|---|---|
| ☐ | Arrancar el LLM | `Salud` o `POST /api/llm/start` | Estado `ok`, puerto 8080 abierto |
| ☐ | Comprobar publicaciones | `Difusiones` | 37 posts enviados; el botón global en «Publicando» |
| ☐ | Elegir modo de ventas | `Finanzas` | Ver sección 5 |
| ☐ | Añadir a los amigos a los grupos | Telegram, con la cuenta de Cuadra | Cada amigo ve las publicaciones |
| ☐ | Asignar identidades | Tabla de la sección 3 | Cada amigo sabe su código `C1`…`C12` |
| ☐ | Repartir los diarios | Fuera del repositorio | Cada amigo tiene su plantilla |
| ☐ | Probar un mensaje real | Chat privado con el bot | El bot responde en segundos |

> El LLM caído es el fallo más probable: el bot publica igual (eso no usa modelo),
> pero no puede contestar a los clientes. `backend/data/logs/llm.log` muestra si
> arrancó y murió después. Si no entra por falta de VRAM, cierra lo que esté
> usando la GPU y vuelve a intentarlo.

## 3. Reparto de identidades

Rellena las dos últimas columnas antes de empezar y pásale a cada amigo solo su
código. No cambies el producto asignado: está elegido para que su zona esté
cubierta por ese TCP.

| Código | Nombre | Zona | Producto asignado | TCP | Amigo | Cuenta de Telegram |
|---|---|---|---|---|---|---|
| C1 | Juan Pérez | Playa | Microondas 20L | Claudia | | |
| C2 | Carlos Rodríguez | Marianao | Ventilador de Torre | Claudia | | |
| C3 | Miguel Torres | Centro Habana | Cafetera Express | Claudia | | |
| C4 | Ana López | La Lisa | Combo Desayuno | Roger | | |
| C5 | Laura Fernández | Marianao | Pizza Personal | Roger | | |
| C6 | Pedro Sánchez | Playa | Monitor LED 24 | Rolando | | |
| C7 | Luis Ramírez | Diez de Octubre | PC de Escritorio Ryzen 5 | Rolando | | |
| C8 | Marta Díaz | Cerro | Aspiradora Portátil | Rolando | | |
| C9 | José Martínez | Habana del Este | Casco para Moto | Yoan | | |
| C10 | Elena Ruiz | Guanabacoa | Moto Eléctrica City | Yoan | | |
| C11 | Daniel Moreno | Regla | Batería para Moto Eléctrica | Yoan | | |
| C12 | Sofía Romero | Plaza de la Revolución | Teclado Mecánico | Rolando | | |

## 4. Grupos y cobertura

Añade a **todos** los clientes a los grupos activos. Los TCP son quienes publican;
los clientes solo tienen que ver los mensajes.

| Grupo | Zonas del grupo | Categorías | Qué se publica ahí |
|---|---|---|---|
| Canal Ventas Electrodomésticos | Todas | tecnología, hogar | Tecnología y hogar de los 4 TCP |
| Venta Electrodomésticos | Todas | tecnología | Tecnología + manuales (accesorios, motos de Yoan) |
| Canal Ventas Alimentos | Todas | alimentos | Comida de Roger |
| Venta Motos E | La Lisa, Marianao | motos_eléctricas | Motos de Roger |
| Venta Motos E Playa | Playa | motos_eléctricas | Motos de Roger (ojo: Roger no entrega en Playa) |
| Venta Motos C | Todas | motos_combustión | Motos de combustión |
| Venta Snacks | Cerro, Marianao, Diez de Octubre | snacks | Snacks de Roger |

Dos avisos para la evaluación:

- **Roger publica en Playa pero no entrega allí.** Es el comportamiento del
  targeting automático (filtra por categoría, no por zona del TCP). Si un cliente
  de Playa pide esa moto, el bot debe rechazar la venta por zona no cubierta.
- **Habana Vieja no la cubre ningún TCP.** Es el caso de zona sin cobertura de la
  guía del cliente; la respuesta correcta es avisar, no aceptar el pedido.

## 5. Modo de ventas: elige antes de empezar

| Modo | Qué pasa al confirmar el cliente | Cuándo usarlo |
|---|---|---|
| **Asistido** (actual) | Venta en «Por aprobar». **El TCP no recibe nada** hasta que pulses «Aprobar» en `Finanzas`. | Si quieres revisar cada venta y probar la aprobación. |
| **Híbrido** | Despacha solo si hay stock, zona cubierta y catálogo reciente; si no, pide aprobación. | Punto medio. |
| **Autónomo** | Despacha al TCP directamente. | **Recomendado para este test**: 20 ventas sin que estés aprobando una por una. |

Si dejas asistido, ten `Finanzas` abierto durante toda la sesión: cada venta
pendiente es un amigo esperando y un TCP que aún no ha recibido la orden.

## 6. Durante la sesión

| Qué vigilar | Dónde | Qué hacer |
|---|---|---|
| Ventas por aprobar | `Finanzas` | Aprobar (asistido/híbrido) o cancelar si el pedido está mal |
| Consultas a TCP pendientes | `Auditoría` / `Logs` | Comprobar que llegan y que se responden |
| Cambios de catálogo | `Auditoría` | Aprobar o rechazar precio/stock propuestos por un TCP |
| Publicaciones | `Difusiones` | Pausar el botón global si algo se descontrola |
| Errores | `Logs` y `Salud` | Revisar antes de tocar nada |

### Los dos casos que coordinas tú

- **T3 (dos órdenes abiertas):** consigue que haya dos ventas en `DISPATCHED`
  antes de decirle al TCP que responda solo `RECIBIDO`.
- **T8 (dos consultas abiertas):** dos escalaciones del mismo producto abiertas
  antes de que responda sin código.

En ambos, avísale por privado cuándo hacerlo. Si no hay dos casos abiertos, el
test no mide ambigüedad.

## 7. Evaluación (R-1…R-5)

| Métrica | Muestra objetivo | De dónde sale | Cómo se mide | Objetivo |
|---|---|---|---|---|
| **R-1** Correlación TCP | 20 mensajes | Diarios `T*` + `Logs` | Comparar a qué orden/consulta dijo el TCP que respondía contra lo que hizo el bot | 0 errores silenciosos; dudas en revisión |
| **R-2** Slots | 50 conversaciones | Diarios `C*` | Exactitud por slot: zona, dirección, teléfono, cantidad, confirmación | Matriz de confusión; sin objetivo fijo |
| **R-3** Ventas E2E | 20 conversaciones | Diarios + tabla `sales` | % de ventas correctas en producto, cantidad, zona y datos | ≥ 80 % |
| **R-4** recall@3 | 100 consultas | Tabla de consultas del cliente | `eval_retrieval.py` con un set real (ver abajo) | ≥ 90 % |
| **R-5** Latencia | ~50 `compose` | BD (`messages.latency_ms`) | `latency_report.py` | p50 en frío ≤ 1,5× caliente; p95 ≤ 8 s |

### Comandos (desde `backend/`)

```powershell
# Ventas por estado
.venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect('data/cuadra.db'); print(c.execute('SELECT status, COUNT(*) FROM sales GROUP BY status').fetchall())"

# Latencia real del modelo
.venv\Scripts\python.exe scripts\latency_report.py --days 1 --purpose compose

# Recall@3 sobre consultas reales etiquetadas
.venv\Scripts\python.exe scripts\eval_retrieval.py --golden-dir ..\cuadra_eval\retrieval_real --tag real --top-k 3
```

Para R-4 necesitas dos archivos en la carpeta del set:

- `products.json`: el catálogo real con la misma forma que
  `backend/tests/golden/products.json` (`nombre`, `descripcion`, `precio`,
  `moneda`, `stock`, `estado`, `zona_entrega`, `especificaciones`).
- `nlu.jsonl`: una línea por consulta, por ejemplo
  `{"id":"C1-q01","text":"cuanto sale el microondas","product":"microondas"}`.
  La clave `product` debe existir en `products.json`.

El set y los diarios viven **fuera del repositorio** (por ejemplo
`C:\Rolando\AProjects\cuadra_eval\`). No los subas a git: llevan conversaciones
reales. El exportador anonimizado todavía no existe; para esta sesión la etiqueta
es el diario y la medición es manual con estas consultas y scripts.

### Riesgos conocidos (no los confundas con fallos del test)

| Riesgo | Qué significa |
|---|---|
| Consulta ambigua | El bot elige la candidata con más puntuación y la marca como ambigua; **no la deja en revisión**. Si el criterio de R-1 exige revisión, esos casos cuentan como fallo. |
| Publicación sin cobertura | Roger publica la moto en Playa pero no entrega allí; el rechazo correcto es por zona. |
| Campañas sin publicar | Cable HDMI, Combo Personalizado y Moto Delivery no salen por diseño. |
| LLM caído | Sin modelo no hay respuestas 1:1; revisar `Salud` antes de empezar. |

## 8. Cierre y reset

| Paso | Acción |
|---|---|
| 1 | Parar el core (`stop_cuadra.ps1`) |
| 2 | Copiar `data/cuadra.db` y `data/logs/*` a una carpeta con fecha |
| 3 | Archivar los diarios en la carpeta externa, ya anonimizados |
| 4 | Borrar `cuadra.db` y `-wal`/`-shm`; archivar los logs |
| 5 | **No borrar** `data/seed_real.json` ni `data/telegram_user.session` |
| 6 | Ejecutar `seed_demo_data.py` y re-anclar peers con `resolve_supplier_peers.py` |
| 7 | Arrancar el core y comprobar `Salud` (LLM incluido) |

## 9. Resultados de la sesión

| Métrica | Muestra conseguida | Resultado | ¿Cumple? | Notas |
|---|---|---|---|---|
| R-1 Correlación TCP | | | | |
| R-2 Slots | | | | |
| R-3 Ventas E2E | | | | |
| R-4 recall@3 | | | | |
| R-5 p95 latencia | | | | |

| Cierre | Respuesta |
|---|---|
| ¿Qué falló más de lo esperado? | |
| ¿Qué caso hay que repetir con más muestra? | |
| ¿Cambiamos algo del seed o de las guías para la próxima? | |
