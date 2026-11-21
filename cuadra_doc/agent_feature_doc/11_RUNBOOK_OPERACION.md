---
tipo: runbook
proyecto: Cuadra
modulo: operacion
estado: FASE 5 - operación documentada
fecha: 2026-11-07
tags: [fase-5, runbook, operacion, backups, incidentes]
---

# 11 - Runbook de operación

> Procedimientos para aprobar, cancelar, revertir, reindexar y restaurar sin
> improvisar. Todo se ejecuta desde `backend/` salvo que se indique otra ruta.
> **Nunca** se corre nada contra `backend/data/cuadra.db` en pruebas: se usa una
> base temporal o el dashboard con el PIN real.

---

## 1. Checklist antes de tocar producción

1. ¿El cambio es de catálogo, de venta o de infraestructura? Cada uno tiene su
   procedimiento (secciones 4, 5 y 7).
2. ¿Hay un backup reciente? Si la respuesta es no, hacerlo primero (sección 8).
3. ¿El cambio se puede deshacer? Las mutaciones de catálogo sí (sección 4.3);
   una venta entregada no se cancela (sección 5.3).
4. ¿Afecta a clientes esperando? Revisar **Auditoría** y **Conversaciones**
   antes de aprobar o rechazar.
5. No editar la base a mano, no renombrar `San Miguel`, no borrar zonas ni grupos.

---

## 2. Arranque y parada

```powershell
# Core (API + cola + Telegram). El modelo arranca solo si CUADRA_LLM_AUTOSTART=1.
cd backend
.\.venv\Scripts\python.exe -m uvicorn cuadra.main:app --host 127.0.0.1 --port 8321

# Solo el servidor llama.cpp (diagnóstico, sin el core)
powershell -ExecutionPolicy Bypass -File .\scripts\start_llm.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\stop_llm.ps1
```

Al arrancar, el core hace solo, en este orden:

1. aplica migraciones pendientes (aditivas);
2. `PRAGMA quick_check`; si falla, registra fallo crítico y hay que restaurar;
3. devuelve a `PENDING` los jobs que quedaron `IN_PROGRESS`;
4. rearma turnos abandonados y despachos de venta pendientes;
5. enciende la cola y los canales.

No hace falta limpiar `jobs` tras un apagón: el arranque es idempotente.

---

## 3. Salud y estado

| Qué | Dónde |
|---|---|
| Estado general, cola, latencias | Panel **General** |
| Modelo cargado / contexto | `GET /api/llm/status` |
| Cola y jobs | Panel **General** o `GET /api/dashboard/summary` |
| Mutaciones pendientes | Panel **Auditoría** |
| Ventas y comisiones | Panel **Finanzas** |
| Errores y eventos | Panel **Logs** |

Comprobación rápida por consola:

```powershell
$pin = "<PIN configurado>"
curl.exe -s -H "X-PIN: $pin" http://127.0.0.1:8321/api/llm/status
curl.exe -s -H "X-PIN: $pin" http://127.0.0.1:8321/api/dashboard/summary
```

---

## 4. Mutaciones de catálogo (Fase 4)

El TCP manda un mensaje; el backend lo triagea y decide:

- **APPLIED**: dentro de política (precio ±50 %, confianza ≥ 0.85, sin ambigüedad).
- **PENDING_REVIEW**: ambigüedad, `MLC`, cambio de moneda, zonas, precio fuera de
  rango o confianza baja. **Nunca** se aplica sola.
- **REJECTED**: el operador la descartó.

### 4.1 Aprobar una mutación en revisión

Panel **Auditoría** → tarjeta en `PENDING_REVIEW` → **Aprobar**. Equivalente API:

```powershell
curl.exe -s -X POST -H "X-PIN: $pin" `
  http://127.0.0.1:8321/api/catalog/mutations/<id>/approve
```

Al aprobar se aplica el cambio, se reindexa el producto y se reanudan **todas**
las conversaciones `WAITING_TCP` de ese producto.

### 4.2 Rechazar

Panel **Auditoría** → **Rechazar** (con motivo). La escalación queda `ANSWERED`
y el cliente recibe una respuesta genérica; el mensaje crudo del TCP **no** se
reenvía.

```powershell
curl.exe -s -X POST -H "X-PIN: $pin" -H "Content-Type: application/json" `
  -d '{"reason":"precio no confirmado"}' `
  http://127.0.0.1:8321/api/catalog/mutations/<id>/reject
```

### 4.3 Deshacer una mutación aplicada

Panel **Auditoría** → tarjeta `APPLIED` → **Deshacer**.

```powershell
curl.exe -s -X POST -H "X-PIN: $pin" -H "Content-Type: application/json" `
  -d '{"reason":"error del TCP"}' `
  http://127.0.0.1:8321/api/catalog/mutations/<id>/revert
```

- Si el producto cambió después (otra mutación, edición manual), el backend
  responde **409 Conflict** y **no** revierte nada. Revisar el estado actual y
  decidir a mano: el sistema no pisa cambios posteriores.
- Deshacer una mutación no borra historial: deja una fila nueva en
  `catalog_mutation_log` con el estado `REVERTED`.

### 4.4 Qué no se puede hacer desde el panel

- Editar el valor propuesto (si el precio está mal, se edita el producto y se
  rechaza la mutación).
- Aceptar `REMOVED` desde el TCP: retirar un producto es decisión del operador.
- Guardar `MLC`: solo `USD` y `CUP`; una propuesta en `MLC` va a revisión.

---

## 5. Ventas y comisiones (Fase 3)

Estados: `DRAFT → PENDING_APPROVAL → PENDING_DISPATCH → DISPATCHED → CONFIRMED
→ DELIVERED → SETTLED`, más `CANCELLED` y `NEEDS_OPERATOR`.

### 5.1 Aprobar y despachar

En modo `assisted`, el operador aprueba desde **Finanzas**:

```powershell
curl.exe -s -X POST -H "X-PIN: $pin" `
  http://127.0.0.1:8321/api/finance/approve/<sale_id>
```

Se encola el despacho al TCP por Telegram. Si el envío falla, el job se reintenta;
tras un reinicio, `recover_pending_dispatches()` rearma los que quedaron en
`PENDING_DISPATCH` sin duplicarlos.

### 5.2 Reintentar un despacho con problema

Si la venta está en `NEEDS_OPERATOR` (por ejemplo, el TCP dijo que no había
stock), corregir la causa y rearmar:

```powershell
curl.exe -s -X POST -H "X-PIN: $pin" `
  http://127.0.0.1:8321/api/finance/dispatch/<sale_id>
```

### 5.3 Cancelar

Solo antes de `DELIVERED`. Libera el stock reservado (nunca por debajo de 0) y
deja el ledger en `VOID`.

```powershell
curl.exe -s -X POST -H "X-PIN: $pin" -H "Content-Type: application/json" `
  -d '{"reason":"el cliente se arrepintió"}' `
  http://127.0.0.1:8321/api/finance/cancel/<sale_id>
```

Una venta `DELIVERED` o `SETTLED` **no** se cancela: responder **409**. En ese
caso se documenta el incidente y se resuelve por conciliación manual.

### 5.4 Cobrar la comisión

```powershell
curl.exe -s -X POST -H "X-PIN: $pin" `
  http://127.0.0.1:8321/api/finance/settle/<sale_id>
```

El ledger pasa de `PENDING` a `COLLECTED`. Si la moneda de la comisión no
coincide con la de la venta, `supplier_payout` queda nulo y exige conversión
manual; el sistema **no** mezcla monedas.

---

## 6. Reindexado

- Guardar un producto desde el dashboard encola `reindex_product` solo.
- Aprobar una mutación encola `reindex_product` (prioridad 90).
- Al arrancar, si falta el modelo de embeddings no se descarga ni se indexa:
  el bot sigue con FTS5 + fuzzy.

Forzar un reindexado completo (solo si el modelo está vendorizado):

```powershell
cd backend
.\.venv\Scripts\python.exe -c "from cuadra.ai import retrieval; print(retrieval.queue_reindex_all())"
```

El job es de prioridad de mantenimiento: no compite con respuestas a clientes.

---

## 7. Reinicio a mitad de turno

1. Parar el core con `Ctrl+C` (cierre ordenado) o cerrar la ventana.
2. Al arrancar de nuevo, el core devuelve los jobs `IN_PROGRESS` a `PENDING`,
   rearma turnos `OPEN`/`PROCESSING` sin job vivo y reanuda despachos pendientes.
3. Verificar en **Logs** los eventos `jobs_requeued` y en **Conversaciones** que
   no haya respuestas duplicadas.

Regla de oro: **no** borrar `jobs`, `turns` ni `messages` para "destrabar". Si la
cola está atascada, revisar el job concreto y su `last_error`.

---

## 8. Backup y restauración

Nunca copiar `cuadra.db` a pelo mientras el core corre: usar la API de backup de
SQLite, que es consistente con WAL.

### 8.1 Backup

```powershell
cd backend
New-Item -ItemType Directory -Force backups | Out-Null
sqlite3 data\cuadra.db ".backup backups\cuadra_$(Get-Date -Format yyyyMMdd).db"
robocopy data\fotos backups\fotos /MIR
```

Retener los últimos 7 días. `backups\` puede vivir en un disco externo.

### 8.2 Restauración

1. Parar el core (y el bot) para que nadie escriba.
2. Mover el `cuadra.db` dañado a `data\cuadra.db.bad`.
3. Copiar el último backup bueno a `data\cuadra.db`.
4. Borrar `data\cuadra.db-wal` y `data\cuadra.db-shm` (pertenecen a la base
   anterior).
5. Arrancar el core: aplica migraciones pendientes y corre `PRAGMA quick_check`.
6. Verificar en **Logs** que no haya error de integridad y que las colas arranquen.

Si el `quick_check` del backup también falla, probar el anterior. No editar el
archivo con un editor hexadecimal.

---

## 9. Incidentes

| Síntoma | Procedimiento |
|---|---|
| El modelo no responde | El bot usa reglas/plantillas y el job reintenta. Revisar `GET /api/llm/status`; reiniciar el modelo (`/api/llm/restart`) sin tocar la cola. |
| El bot repite precios internos | Revisar **Logs**: el guardrail debió marcar `internal_terms` o `prompt_disclosure`. No borrar el evento; abrir incidente y revisar el prompt/plantilla. |
| Mutación con valor absurdo | **Rechazar** la mutación; si ya se aplicó, **Deshacer**. Si el undo da 409, corregir el producto a mano y documentar. |
| Cliente esperando al TCP | Revisar **Auditoría**: la escalación debe seguir `PENDING`. Al aprobar o rechazar se reanudan las conversaciones `WAITING_TCP`. |
| Venta despachada que el TCP no confirma | Esperar el ack `[V-######]`; si no llega, revisar **Finanzas** y reenviar el despacho. No marcar `DELIVERED` sin confirmación. |
| BD corrupta | Restaurar el último backup (sección 8.2) y registrar el incidente. |
| Cola atascada | Identificar el job y su `last_error`; no vaciar tablas. |

---

## 10. Agente L3: tools, grounding y watchdog

### 10.1 Ver el modo actual

- `AGENT_TOOLS_MODE`: `off` (default), `shadow` u `on`.
- Modo por dominio: `AGENT_TOOLS_CATALOG_MODE`, `..._SALES_MODE`,
  `..._MEMORY_MODE`, `..._ESCALATIONS_MODE`.
- El planner solo corre si el global y el dominio están en `shadow`/`on`.
- `off`: ruta determinista, sin llamada al planner.
- `shadow`: el modelo propone; se registra y compara; la respuesta no cambia.
- `on`: se ejecutan las lecturas válidas (solo `read`); la política
  determinista sigue siendo el fallback y el validador.

### 10.2 Apagar una tool o volver a `off`

- Apagar un dominio: `CUADRA_AGENT_TOOLS_CATALOG_MODE=off` (o el dominio que
  corresponda) y reiniciar el core.
- Apagar todo el planner: `CUADRA_AGENT_TOOLS_MODE=off`.
- Sin reiniciar el flujo: `config.AGENT_TOOLS_MODE` y
  `config.AGENT_TOOLS_<DOMINIO>_MODE` se leen en cada turno; cambiarlos en
  caliente vuelve a `off` en el siguiente turno.

### 10.3 Leer una traza de tool

- Panel **Trazas** o SQL:

  ```sql
  SELECT id, created_at, method, ok, duration_ms, output_json
  FROM traces
  WHERE method LIKE 'tool.%'
  ORDER BY id DESC LIMIT 20;
  ```

- `output_json.cache_hit` indica que la lectura vino del cache del turno.
- `orchestrator.tool_plan` guarda la propuesta del modelo;
  `orchestrator.tool_plan_shadow` compara la propuesta con la evidencia
  determinista.

### 10.4 Grounding y watchdog

- Violaciones de grounding: `events` con `type='grounding_violation'`.
  En `shadow` se registran y la respuesta no cambia.
- Watchdog: `events` con `type='turn_timeout'`; `AGENT_TURN_WATCHDOG_S=180`
  (0 lo apaga).
- El panel **Métricas** muestra llamadas a tools, errores, cache hit,
  violaciones de grounding y watchdogs.

### 10.5 Reproducir un fallo

1. `scripts/replay_turns.py` para la conversación sintética (sin Telegram).
2. `scripts/eval_tools.py` para el golden set del planner.
3. `scripts/eval_agent.py --suite offline` para grounding y referencias
   (no necesita modelo).
4. `scripts/eval_agent.py --suite all` para NLU/compose con el modelo local.
5. Añadir el caso al golden set antes de corregir.

---

## 11. Prohibiciones

- No borrar grupos de difusión: se desactivan.
- No borrar zonas ni proveedores: se desactivan (`active = 0`).
- No renombrar `San Miguel`.
- No editar snapshots de mutaciones en la base.
- No correr migraciones ni pruebas contra la base real.
- No exponer el PIN, tokens de Telegram ni `seed_real.json`.
- No hacer commit de secretos ni de `backend/data/`.
