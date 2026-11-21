---
tipo: guia_evaluacion
proyecto: Cuadra
modulo: agente_ia
estado: listo para ejecutar
revisado: 2026-11-21
tags: [L3, evaluacion, dashboard, seed, TCP, cliente]
---

# 16 — Evaluación L3

> Runbook corto. Se hace en orden, sin rutas alternativas.

## 0. Arranque en cero

1. Para el core: `.\stop_cuadra.ps1`.
2. Desde la raíz del repo, borra la base y los logs:
   ```powershell
   Remove-Item backend\data\cuadra.db* -Force -ErrorAction SilentlyContinue
   Remove-Item backend\data\logs\* -Recurse -Force -ErrorAction SilentlyContinue
   ```
3. Borra el chat con el bot en la cuenta **cliente** y en la cuenta **TCP**.
4. En `backend/`, ejecuta el seed con el id numérico del TCP:
   ```powershell
   cd backend
   .\.venv\Scripts\python.exe scripts\seed_l3_eval.py --telegram-id 123456789 --telegram-handle "@mi_tcp"
   ```
   - `123456789` es el **id numérico** de la cuenta TCP; lo da `@userinfobot`
     desde esa cuenta.
   - `@mi_tcp` es el `@usuario` del TCP. **Ponlo entre comillas**: PowerShell
     interpreta `@` como splatting y se come el argumento.
   - Si no tienes el id a mano, ejecuta solo
     `scripts\seed_l3_eval.py` y enlaza el TCP luego en el dashboard.
5. Arranca el core: `.\start_cuadra.ps1 -Background`.
6. Abre `http://127.0.0.1:8321` (PIN `0000`).
7. **Catálogo → Proveedores → `EVAL TCP`:** verifica que tenga tu `@usuario` y
   el peer id. Zonas: Playa y Marianao cubiertas; Centro no.

Qué deja el seed: `EVAL TCP`, 28 productos `EVAL ...`, 2 clientes `eval-*`,
6 ventas, 4 escalaciones, hechos y contexto. Los clientes `eval-*` son
**fixtures de la base de datos**, no cuentas de Telegram: los ves en el
dashboard, pero la prueba la haces con tu cuenta cliente real.

## 1. La prueba

### 1.1 Cliente — catálogo y referencias

| Envía | Espera |
|---|---|
| «¿qué accesorios tienes?» | tabla con 3 productos, incluido EVAL Teclado agotado; **no** la laptop |
| «el segundo» | ficha del segundo producto del listado que acabas de ver |
| «¿qué productos tienes de EVAL Doce?» | 10 de 12, sin relleno |
| «¿tienen neveras?» | no inventa; pregunta u ofrece alternativa |

### 1.2 Cliente — zonas y fecha

| Envía | Espera |
|---|---|
| «¿entregan en Centro?» | no cubierto; no acepta la venta |
| «¿entregan en Playa?» | cubierto |
| «¿cuándo llega?» | no inventa fecha; se coordina al confirmar |

### 1.3 Cliente — memoria

| Envía | Espera |
|---|---|
| «me llamo Cliente Prueba, vivo en Playa, calle 1 #2, mi teléfono es 55500001» | lo toma (o lo usa al pedir los datos de la venta) |
| «¿ya me dijiste mi dirección?» | la recuerda; no la pide de nuevo |

### 1.4 Cliente + TCP — escalación

| Paso | Quién | Qué hace | Espera |
|---|---|---|---|
| 1 | Cliente | «¿la EVAL Laptop tiene garantía de 2 años?» | «le consulto al almacén…»; no inventa |
| 2 | TCP | recibe un mensaje con `[E-…]` | responde el dato citando `[E-…]` |
| 3 | Cliente | espera la respuesta | «Ya me confirmó el almacén…» |
| 4 | Cliente | «¿el proveedor ya respondió?» | si ya respondió, lo dice; si no, no promete |

### 1.5 Cliente + TCP — venta

| Paso | Quién | Qué hace | Espera |
|---|---|---|---|
| 1 | Cliente | «quiero el EVAL Mouse» | precio y oferta de reserva |
| 2 | Cliente | «sí» | pide nombre, zona, dirección y teléfono |
| 3 | Cliente | responde los datos que pida: nombre «Cliente Prueba», zona «Playa», dirección «Calle 1 #2», teléfono «55500001» | venta creada; el TCP recibe `V-…` |
| 4 | TCP | `RECIBIDO V-…` | la venta cambia de estado en Finanzas |
| 5 | Cliente | «¿ya llegó mi pedido?» | estado real (pendiente/confirmado), no inventado |

## 2. Al final, mira esto

| Panel | Qué revisar |
|---|---|
| **Conversaciones → detalle** | el chat real, los hechos, la escalación |
| **Finanzas** | la venta y su estado |
| **Métricas → Herramientas** | llamadas/turno, errores, cache hit, grounding, watchdogs, top tools |
| **Trazas** | `tool.*` y, si activaste el planner, `orchestrator.tool_plan` |

## 3. Comprobación rápida (opcional, sin Telegram)

```powershell
cd backend
.\.venv\Scripts\python.exe scripts\eval_tools.py
.\.venv\Scripts\python.exe scripts\eval_agent.py --suite offline
```

Esperado: `100 % / 100 %` en tools; `15/15` en grounding y `8/8` en referencias.

## 4. Cierre

- [ ] Los cinco bloques de §1 pasan.
- [ ] La venta y la escalación cambian de estado.
- [ ] No hay `grounding_violation` ni `turn_timeout` raros en el dashboard.
- [ ] Decides si el planner queda en `off`, `shadow` u `on`.
- [ ] Apruebas el merge/push (no se hace sin pedido explícito).

## 5. Si algo falla

| Síntoma | Qué revisar |
|---|---|
| Responde genérico o no lista | el core está corriendo; mira `orchestrator.tool_plan`/`tool.*` en Trazas |
| El TCP no recibe | `telegram_peer_id` en Proveedores; que la cuenta TCP haya escrito al bot |
| No crea escalación | producto/proveedor activo; fila en `escalations` |
| Venta atascada | estado en Finanzas; respuesta del TCP; traza `sales.apply_tcp_ack` |
| «No hay X» sin evidencia | evento `grounding_violation`; en `shadow` no bloquea la respuesta |
| El bot no responde | core y modelo arriba; `CUADRA_TELEGRAM_ENABLED=1` |
