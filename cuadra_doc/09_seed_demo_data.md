---
tipo: setup
proyecto: Cuadra
estado: operativo
actualizado: 2026-10-31
tags: [seed, demo, testing, datos, script]
---

# 09 — Seed de datos demo

`backend/scripts/seed_demo_data.py` crea desde cero toda la data base necesaria para probar Cuadra: las zonas de La Habana, los TCP, los grupos de Telegram con su configuración, y un catálogo fake de categorías, productos y difusiones. Está pensado para una base de datos nueva o para añadir data de prueba sin tocar la data real.

No forma parte del arranque normal de la aplicación: es una herramienta manual de desarrollo.

## 1. Dos archivos, una sola idea

El seed vive en git, pero **los valores sensibles no**. Solo hay dos datos que no se versionan:

| Dato sensible | Dónde vive |
| --- | --- |
| `telegram_id` de cada TCP | `backend/data/seed_real.json` |
| `target` (enlace de invitación) de cada grupo | `backend/data/seed_real.json` |

Todo lo demás (nombres, alias, `peer_id`, estados, zonas, categorías, productos y difusiones) se queda en el script y sí se versiona.

`backend/data/` está en `.gitignore`, así que `seed_real.json` nunca aparece en `git status`. El seed lo carga al arrancar, lo cruza por nombre y falla con un mensaje claro si falta el archivo o alguna clave.

## 2. Requisitos

- Ejecutar desde `backend/` con el virtualenv del proyecto.
- Tener creado `backend/data/seed_real.json` (ver sección 3).
- No hace falta arrancar la app antes: el script aplica las migraciones pendientes por sí solo.

## 3. Archivo local `backend/data/seed_real.json`

Es un JSON con dos objetos. Las claves son los nombres exactos que aparecen en el seed (`SUPPLIERS[*].name` y `GROUPS[*].name`):

```json
{
  "suppliers": {
    "Claudia": {
      "telegram_id": "@xxxxxxxxxxxx"
    },
    "Roger": {
      "telegram_id": "@xxxxxxxxxxxx"
    },
    "Rolando": {
      "telegram_id": "@xxxxxxxxxxxx"
    },
    "Yoan": {
      "telegram_id": "@xxxxxxxxxxxx"
    }
  },
  "groups": {
    "Canal Ventas Electrodomésticos": {
      "target": "https://t.me/+xxxxxxxxxxxx"
    },
    "Canal Ventas Alimentos": {
      "target": "https://t.me/+xxxxxxxxxxxx"
    }
  }
}
```

Reglas:

- Los nombres deben coincidir **exactamente** (tildes incluidas).
- Cada TCP del seed necesita su `telegram_id`; cada grupo necesita su `target`.
- Se admiten claves extra: el script avisa con un `Warning: unknown supplier/group ...` (útil para detectar erratas).
- El archivo se lee como UTF-8 (con o sin BOM).
- Si falta algo, el script no escribe nada y devuelve código de salida `1` listando lo que falta.

## 4. Uso

Desde `backend/`:

```powershell
# Crear toda la data en la base configurada
.venv\Scripts\python.exe scripts\seed_demo_data.py

# Ver qué se crearía, sin escribir nada
.venv\Scripts\python.exe scripts\seed_demo_data.py --dry-run

# Forzar que las difusiones nuevas queden vencidas de inmediato
.venv\Scripts\python.exe scripts\seed_demo_data.py --schedule now

# Usar otro archivo de datos reales
.venv\Scripts\python.exe scripts\seed_demo_data.py --real-data C:\ruta\seed_real.json
```

Flags:

| Flag | Valores | Por defecto | Qué hace |
| --- | --- | --- | --- |
| `--dry-run` | — | desactivado | Recorre todo, imprime lo que crearía y hace `rollback` |
| `--schedule` | `auto`, `now`, `future` | `auto` | Programa `next_send_at` de las difusiones |
| `--real-data` | ruta a un JSON | `backend/data/seed_real.json` | Archivo local con los valores sensibles |

Para validar contra una base desechable sin tocar la real, apunta el directorio de datos:

```powershell
$env:CUADRA_DATA_DIR = Join-Path $env:TEMP "cuadra_seed_test"
New-Item -ItemType Directory -Path $env:CUADRA_DATA_DIR -Force | Out-Null
.venv\Scripts\python.exe scripts\seed_demo_data.py
```

## 5. Qué crea

| Tabla | Contenido |
| --- | --- |
| `zones` | 16 municipios de La Habana (`UCI` inactiva) |
| `categories` | 8 categorías: `tecnología`, `computación`, `accesorios`, `hogar`, `snacks`, `motos_eléctricas`, `motos_combustión`, `alimentos` |
| `suppliers` | 4 TCP reales |
| `broadcast_groups` | 9 grupos: 7 activos y 2 en `PAUSED` |
| `group_categories` | 8 mapeos grupo → categoría |
| `group_zones` | 68 mapeos grupo → zona |
| `supplier_delivery_zones` | 20 mapeos TCP → zona |
| `products` | 32 productos fake (31 `ACTIVE` + 1 `OUT_OF_STOCK`) |
| `broadcasts` | 25 difusiones (24 `SCHEDULED` habilitadas + 1 deshabilitada) |
| `broadcast_group_links` | 16 selecciones manuales de grupos |

Los productos y difusiones propios del usuario **no** se siembran: el seed solo trae su catálogo demo. La data real que sí se respeta es la de canales, grupos, TCP y zonas.

Los dos grupos deshabilitados se guardan como `PAUSED` para que el scheduler los ignore, pero siguen existiendo para probar filtros.

### 5.1 Escenarios cubiertos a propósito

Las difusiones no son solo “muchas”: cada una cubre una rama de la lógica de selección y publicación.

| Difusión | Configuración | Qué evalúa |
| --- | --- | --- |
| `Laptop Empresarial`, `Moto Eléctrica Urban`, `Combo Desayuno`, `Papas Fritas` | Auto | Selección automática por categoría con grupos compatibles |
| `Monitor LED 24`, `Moto Eléctrica Cargo` | Manual, uno o varios grupos activos | Selección manual y múltiples destinos |
| `Cable HDMI 2 m` | Auto, categoría `accesorios` | Ningún grupo acepta esa categoría → sin destinos |
| `Combo Personalizado` | Auto, producto **sin categoría** | Solo apunta a grupos sin restricciones; hoy no hay ninguno activo → sin destinos |
| `Power Bank 20000` | Manual, producto `OUT_OF_STOCK` | El estado del producto no bloquea la publicación |
| `Cafetera Express` | Manual, un grupo activo + uno `PAUSED` | El grupo pausado se ignora; el activo sí publica |
| `Moto Eléctrica Delivery` | Manual, solo un grupo `PAUSED` | Sin destinos → la campaña queda `FAILED` |
| `Laptop Gamer RTX 3050` | Auto + plantilla con spintax, `{zona}` e `{id}` | Render de spintax, zonas del TCP y que no se añada el sufijo `ID` |
| `Moto Combustión Zontes 250` | Manual, categoría que el grupo no permite | La selección manual ignora la categoría del grupo |
| `Mouse Inalámbrico` | `enabled = 0` | Toggle de publicación deshabilitado: sigue visible y editable, pero el scheduler la ignora |

Las campañas sin destinos se crean como `SCHEDULED`; el scheduler las pasa a `FAILED` en su primera evaluación (`broadcast_no_targets`). Eso permite probar también el panel de logs y el filtro por estado.

## 6. Idempotencia

Todo se busca por clave natural antes de insertar:

- zonas, categorías, TCP y productos → por `name`
- grupos → por `channel` + `target`
- difusiones → por `template`
- mapeos → por el par de IDs

Una segunda ejecución reporta `0` filas creadas en todas las tablas. Se puede correr las veces que haga falta sobre la misma base.

## 7. Programación de difusiones

`--schedule auto` (por defecto) decide según el estado de la tabla `broadcasts`:

- Si la tabla está vacía, las difusiones quedan vencidas de inmediato (`next_send_at = NULL`), listas para que el scheduler las publique.
- Si ya hay difusiones, se escalonan 4 horas hacia el futuro (`+4h`, `+5h`, ...) para que un scheduler en marcha no publique mientras se siembra.

`--schedule now` fuerza siempre “vencida ahora”; `--schedule future` fuerza siempre el escalonado futuro. Las difusiones deshabilitadas nunca reciben `next_send_at`.

## 8. Zonas de grupos (caso de prueba)

Las zonas de los grupos se asignaron a propósito con combinaciones distintas:

- Los grupos “Todas” tienen los 15 municipios activos.
- `Venta Motos E` → La Lisa y Marianao (zonas de Roger).
- `Venta Motos E Playa` → solo Playa.
- `Venta Snacks` → Cerro, Marianao y Diez de Octubre.

Esto permite evaluar el caso de un TCP que reparte en La Lisa/Marianao y un grupo que solo publica en Playa.

> **Importante:** hoy `eligible_groups` en `backend/cuadra/domain/broadcasts.py` solo filtra por `status = 'ACTIVE'` y categoría del producto. **Las zonas todavía no intervienen en la elegibilidad**, así que el bot sí publicaría en un grupo cuya zona no coincide con la del TCP. El seed deja los datos listos para probarlo, pero el filtro hay que implementarlo aparte.

## 9. Añadir más data fake

- **Categoría nueva:** agrégala a `CATEGORIES`.
- **Producto nuevo:** añádelo a `DEMO_PRODUCTS`; `supplier` y `category` se resuelven por nombre. `status` es opcional y por defecto es `ACTIVE` (acepta `OUT_OF_STOCK`, `PAUSED`, `REMOVED`). Para un producto sin categoría usa `"category": None`.
- **Difusión nueva:** añádela a `DEMO_BROADCASTS`. `groups: None` usa selección automática por categoría; una lista de nombres crea enlaces manuales (aunque el grupo esté `PAUSED`, se ignora al publicar). El texto por defecto sale de `TEMPLATE`; puedes pasar una `template` propia por difusión.
- **Grupo nuevo:** añádelo a `GROUPS` y su `target` al JSON local. Si el grupo tiene `peer_id` conocido, ponlo en el seed; si no, déjalo en `None` y Telethon lo resolverá en el primer envío (la cuenta debe ser miembro).
- **Mapeos:** agrega entradas a `GROUP_CATEGORIES`, `GROUP_ZONES` o `SUPPLIER_DELIVERY_ZONES`.

Después de cualquier cambio, valida con `--dry-run` y luego corre el seed dos veces sobre una base temporal.

## 10. Problemas frecuentes

**`Real data file not found`**
Falta `backend/data/seed_real.json`. Créalo con el esquema de la sección 3.

**`is missing: suppliers['...'].telegram_id, groups['...'].target`**
El archivo existe pero le falta una clave o el nombre no coincide exactamente. Revisa tildes y mayúsculas.

**`Warning: unknown supplier/group`**
Hay una clave en el JSON que no existe en el seed. Suele ser una errata; no impide la ejecución, pero ese valor no se usará.

**`Invalid JSON`**
Comprueba comas, comillas y llaves. El archivo puede tener BOM; el script lo admite.

**Los productos no aparecen**
Solo se crean si su `supplier` y su `category` existen. Revisa los mensajes `Skipping ...: supplier/category not found`.

**Una difusión no se crea**
Su producto debe existir y todos los grupos de `groups` deben existir; si falta uno, la difusión se omite completa.

## 11. Relación con la base real

El seed está pensado para bases nuevas y de prueba. Sobre una base con data real no sobrescribe filas existentes (todo es “crear si falta”), pero sí puede añadir productos y difusiones demo. Antes de correrlo contra `backend/data/cuadra.db`, haz una copia o usa `CUADRA_DATA_DIR` con una carpeta temporal.
