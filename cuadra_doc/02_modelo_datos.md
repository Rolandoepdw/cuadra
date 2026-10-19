---
tipo: modelo_datos
proyecto: Cuadra
estado: diseño Fase 1
actualizado: 2026-10-19
tags: [sqlite, schema, ventas, comisiones]
---

# 02 — Modelo de Datos (SQLite)

> Principio rector: **el producto es una presentación abstracta del TCP**. No existe catálogo canónico; dos TCPs ofreciendo "lo mismo" son dos filas distintas que el agente puede comparar en tiempo de respuesta.

## 1. Entidades y relaciones (visión)

```
tcps 1───N productos 1───N producto_imagenes
categorias 1───N productos
tcps 1───N ventas N───1 clientes
clientes 1───N cliente_canales
clientes 1───N conversaciones 1───N mensajes
conversaciones 1───N escalados
conversaciones 1───N ventas            (venta nace de una conversación)
grupos_difusion N───N categorias       (grupo_categorias)
grupos_difusion 1───N envios_difusion  (log de lo publicado)
difusiones 1───N envios_difusion
difusiones N───1 productos (opcional)
trabajos                              (cola persistente, sin FK)
eventos                               (auditoría, sin FK)
```

## 2. Esquema SQL (migración `001_init.sql`)

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- =========================================================
-- PROVEEDORES (TCP)
-- =========================================================
CREATE TABLE tcps (
    id                INTEGER PRIMARY KEY,
    nombre            TEXT NOT NULL,              -- nombre del socio
    alias_negocio     TEXT,                       -- cómo firma su WhatsApp/Telegram
    telefono_wa       TEXT,                       -- Fase 2: consultas por WhatsApp
    telegram_id       TEXT,                       -- Fase 1: escalado por Telegram
    zona_entrega      TEXT,                       -- p. ej. "Playa, Habana Vieja"
    condiciones_pago  TEXT,
    prioridad         INTEGER DEFAULT 100,        -- menor = preferido al empatar
    notas             TEXT,
    activo            INTEGER DEFAULT 1,
    creado_en         TEXT DEFAULT (datetime('now')),
    actualizado_en    TEXT DEFAULT (datetime('now'))
);

-- =========================================================
-- PRODUCTOS (presentación de UN TCP; sin catálogo canónico)
-- =========================================================
CREATE TABLE productos (
    id                    INTEGER PRIMARY KEY,
    tcp_id                INTEGER NOT NULL REFERENCES tcps(id),
    categoria_id          INTEGER REFERENCES categorias(id),  -- NULL = solo grupos sin restricción
    nombre                TEXT NOT NULL,          -- como el TCP lo presenta
    descripcion           TEXT,
    precio                REAL NOT NULL,
    moneda                TEXT NOT NULL DEFAULT 'USD'
                          CHECK (moneda IN ('USD', 'CUP')),
    comision              REAL NOT NULL,          -- monto que cobra Cuadra
    moneda_comision       TEXT NOT NULL DEFAULT 'USD'
                          CHECK (moneda_comision IN ('USD', 'CUP')),
    stock                 INTEGER DEFAULT 1,
    estado                TEXT NOT NULL DEFAULT 'ACTIVO'
                          CHECK (estado IN ('ACTIVO', 'AGOTADO', 'PAUSADO', 'BAJA')),
    -- memoria dinámica: se enriquece con cada respuesta del TCP
    especificaciones_json TEXT NOT NULL DEFAULT '{}',
    creado_en             TEXT DEFAULT (datetime('now')),
    actualizado_en        TEXT DEFAULT (datetime('now'))
);
CREATE INDEX idxProductos_tcp ON productos(tcp_id);
CREATE INDEX idxProductos_estado ON productos(estado);

CREATE TABLE producto_imagenes (
    id          INTEGER PRIMARY KEY,
    producto_id INTEGER NOT NULL REFERENCES productos(id) ON DELETE CASCADE,
    ruta_local  TEXT NOT NULL,                    -- data/fotos/producto_12/img_001.jpg
    es_principal INTEGER DEFAULT 0,
    orden       INTEGER DEFAULT 0
);
CREATE INDEX idxImagenes_producto ON producto_imagenes(producto_id);

-- =========================================================
-- CLIENTES (identidad multi-canal)
-- =========================================================
CREATE TABLE clientes (
    id        INTEGER PRIMARY KEY,
    nombre    TEXT,                               -- aprendido de la conversación
    preferencias_json TEXT DEFAULT '{}',          -- zona, moneda habitual, etc.
    notas     TEXT,
    creado_en TEXT DEFAULT (datetime('now'))
);

CREATE TABLE cliente_canales (
    id           INTEGER PRIMARY KEY,
    cliente_id   INTEGER NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    canal        TEXT NOT NULL CHECK (canal IN ('telegram', 'whatsapp')),
    identificador TEXT NOT NULL,                  -- telegram user_id / teléfono (JID)
    UNIQUE(canal, identificador)
);

-- =========================================================
-- CONVERSACIONES Y MENSAJES (historial = contexto del LLM)
-- =========================================================
CREATE TABLE conversaciones (
    id             INTEGER PRIMARY KEY,
    cliente_id     INTEGER NOT NULL REFERENCES clientes(id),
    canal          TEXT NOT NULL CHECK (canal IN ('telegram', 'whatsapp')),
    estado         TEXT NOT NULL DEFAULT 'ACTIVA'
                   CHECK (estado IN ('ACTIVA', 'ESPERANDO_TCP', 'CERRADA')),
    -- producto en discusión, intención detectada, último saludo, etc.
    contexto_json  TEXT NOT NULL DEFAULT '{}',
    creada_en      TEXT DEFAULT (datetime('now')),
    actualizada_en TEXT DEFAULT (datetime('now'))
);
CREATE INDEX idxConversaciones_estado ON conversaciones(estado);

CREATE TABLE mensajes (
    id              INTEGER PRIMARY KEY,
    conversacion_id INTEGER NOT NULL REFERENCES conversaciones(id) ON DELETE CASCADE,
    direccion       TEXT NOT NULL CHECK (direccion IN ('ENTRANTE', 'SALIENTE')),
    -- quién produjo el mensaje saliente (el ENTRANTE siempre es 'cliente')
    autor           TEXT NOT NULL DEFAULT 'cliente'
                    CHECK (autor IN ('cliente', 'bot', 'tcp', 'operador')),
    tipo            TEXT NOT NULL DEFAULT 'texto'
                    CHECK (tipo IN ('texto', 'audio', 'imagen', 'documento')),
    cuerpo          TEXT,                         -- texto o transcripción
    ruta_archivo    TEXT,                         -- audio/imagen original en disco
    latencia_ms     INTEGER,                      -- salientes generados por IA
    creado_en       TEXT DEFAULT (datetime('now'))
);
CREATE INDEX idxMensajes_conversacion ON mensajes(conversacion_id, creado_en);

-- =========================================================
-- ESCALADO AL TCP (bucle crítico)
-- =========================================================
CREATE TABLE escalados (
    id                INTEGER PRIMARY KEY,
    conversacion_id   INTEGER NOT NULL REFERENCES conversaciones(id),
    producto_id       INTEGER REFERENCES productos(id),
    tcp_id            INTEGER NOT NULL REFERENCES tcps(id),
    pregunta          TEXT NOT NULL,              -- lo que el cliente preguntó
    mensaje_tcp_enviado TEXT,
    respuesta_tcp     TEXT,
    estado            TEXT NOT NULL DEFAULT 'PENDIENTE'
                      CHECK (estado IN ('PENDIENTE', 'RESPONDIDO',
                                        'RESUELTO', 'CANCELADO', 'TIMEOUT')),
    intentos          INTEGER DEFAULT 0,          -- reconsultas al TCP
    creado_en         TEXT DEFAULT (datetime('now')),
    respondido_en     TEXT,
    resuelto_en       TEXT
);
CREATE INDEX idxEscalados_estado ON escalados(estado);

-- =========================================================
-- VENTAS Y COMISIONES (auditoría económica)
-- Flujo: BORRADOR (bot) → PENDIENTE_APROBACION → CONFIRMADA (operador)
--        → PAGADA (operador)   |   CANCELADA (operador)
-- El cierre del pago SIEMPRE lo hace el operador.
-- =========================================================
CREATE TABLE ventas (
    id               INTEGER PRIMARY KEY,
    conversacion_id  INTEGER REFERENCES conversaciones(id),
    producto_id      INTEGER NOT NULL REFERENCES productos(id),
    tcp_id           INTEGER NOT NULL REFERENCES tcps(id),
    cliente_id       INTEGER NOT NULL REFERENCES clientes(id),
    monto            REAL NOT NULL,               -- precio pactado con el cliente
    moneda           TEXT NOT NULL CHECK (moneda IN ('USD', 'CUP')),
    tipo_comision    TEXT NOT NULL DEFAULT 'FIJA'
                     CHECK (tipo_comision IN ('FIJA', 'PORCENTUAL')),
    comision         REAL NOT NULL,               -- monto calculado (auditable)
    -- precio/tcp en el momento de la venta (foto histórica)
    contexto_json    TEXT NOT NULL DEFAULT '{}',
    estado           TEXT NOT NULL DEFAULT 'BORRADOR'
                     CHECK (estado IN ('BORRADOR', 'PENDIENTE_APROBACION',
                                       'CONFIRMADA', 'PAGADA', 'CANCELADA')),
    origen           TEXT NOT NULL DEFAULT 'bot' CHECK (origen IN ('bot', 'operador')),
    creado_en        TEXT DEFAULT (datetime('now')),
    actualizado_en   TEXT DEFAULT (datetime('now')),
    aprobada_en      TEXT,
    pagada_en        TEXT
);
CREATE INDEX idxVentas_estado ON ventas(estado);
CREATE INDEX idxVentas_tcp ON ventas(tcp_id);

-- =========================================================
-- CATEGORÍAS Y GRUPOS DE DIFUSIÓN (curaduría 100% del operador)
-- El bot NO busca ni depura grupos: se registran desde el dashboard.
-- =========================================================
CREATE TABLE categorias (
    id      INTEGER PRIMARY KEY,
    nombre  TEXT NOT NULL UNIQUE,      -- alimentos, tecnología, construcción, hogar, ...
    activa  INTEGER DEFAULT 1
);

CREATE TABLE grupos_difusion (
    id                INTEGER PRIMARY KEY,
    canal             TEXT NOT NULL CHECK (canal IN ('telegram', 'whatsapp')),
    destino           TEXT NOT NULL,               -- chat id o invite link
    nombre            TEXT NOT NULL,               -- nombre visible del grupo (para el operador)
    estado            TEXT NOT NULL DEFAULT 'ACTIVO'
                      CHECK (estado IN ('ACTIVO', 'PAUSADO', 'PROHIBIDO')),
    max_posts_dia     INTEGER DEFAULT 3,
    min_intervalo_min INTEGER DEFAULT 10,          -- jitter por publicación
    max_intervalo_min INTEGER DEFAULT 40,
    notas             TEXT,                        -- reglas del grupo, horarios, etc.
    creado_en         TEXT DEFAULT (datetime('now')),
    actualizado_en    TEXT DEFAULT (datetime('now')),
    UNIQUE(canal, destino)
);

-- Qué categorías está permitido publicar en cada grupo (muchos-a-muchos)
CREATE TABLE grupo_categorias (
    grupo_id     INTEGER NOT NULL REFERENCES grupos_difusion(id) ON DELETE CASCADE,
    categoria_id INTEGER NOT NULL REFERENCES categorias(id) ON DELETE CASCADE,
    PRIMARY KEY (grupo_id, categoria_id)
);

-- =========================================================
-- DIFUSIONES (campañas; los destinos se calculan al enviar)
-- =========================================================
CREATE TABLE difusiones (
    id             INTEGER PRIMARY KEY,
    canal          TEXT NOT NULL CHECK (canal IN ('telegram', 'whatsapp')),
    plantilla      TEXT NOT NULL,                  -- texto con spintax {A|B}
    producto_id    INTEGER REFERENCES productos(id),
    -- selección manual opcional de grupos; vacío = auto-match por categoría
    estado         TEXT NOT NULL DEFAULT 'PROGRAMADA'
                   CHECK (estado IN ('PROGRAMADA', 'EN_ENVIO', 'COMPLETADA',
                                     'FALLIDA', 'CANCELADA')),
    proximo_envio  TEXT,                           -- próximo disparo (jitter)
    creado_en      TEXT DEFAULT (datetime('now')),
    actualizado_en TEXT DEFAULT (datetime('now'))
);

-- Selección manual explícita de grupos para una difusión (opcional)
CREATE TABLE difusion_grupos (
    difusion_id INTEGER NOT NULL REFERENCES difusiones(id) ON DELETE CASCADE,
    grupo_id    INTEGER NOT NULL REFERENCES grupos_difusion(id) ON DELETE CASCADE,
    PRIMARY KEY (difusion_id, grupo_id)
);

-- Log auditable de cada publicación real (qué salió, a quién, cuándo)
CREATE TABLE envios_difusion (
    id           INTEGER PRIMARY KEY,
    difusion_id  INTEGER REFERENCES difusiones(id),
    grupo_id     INTEGER NOT NULL REFERENCES grupos_difusion(id),
    canal        TEXT NOT NULL,
    texto_enviado TEXT NOT NULL,                   -- ya con spintax resuelto
    estado       TEXT NOT NULL DEFAULT 'ENVIADO'
                 CHECK (estado IN ('ENVIADO', 'FALLIDO')),
    error        TEXT,
    enviado_en   TEXT DEFAULT (datetime('now'))
);
CREATE INDEX idxEnvios_grupo ON envios_difusion(grupo_id, enviado_en);

-- =========================================================
-- TRABAJOS (cola persistente; sobrevive apagones)
-- =========================================================
CREATE TABLE trabajos (
    id               INTEGER PRIMARY KEY,
    tipo             TEXT NOT NULL,               -- transcribir_audio | generar_respuesta |
                                                 -- enviar_mensaje | consultar_tcp | publicar_difusion
    payload_json     TEXT NOT NULL,
    prioridad        INTEGER DEFAULT 100,         -- 10 cliente, 50 TCP, 100 difusión
    estado           TEXT NOT NULL DEFAULT 'PENDIENTE'
                     CHECK (estado IN ('PENDIENTE', 'EN_PROCESO', 'COMPLETADO', 'FALLIDO')),
    intentos         INTEGER DEFAULT 0,
    max_intentos     INTEGER DEFAULT 3,
    proximo_reintento TEXT,                       -- backoff exponencial
    error_ultimo     TEXT,
    creado_en        TEXT DEFAULT (datetime('now')),
    actualizado_en   TEXT DEFAULT (datetime('now'))
);
CREATE INDEX idxTrabajos_cola ON trabajos(estado, proximo_reintento, prioridad);

-- =========================================================
-- EVENTOS (auditoría del sistema)
-- =========================================================
CREATE TABLE eventos (
    id          INTEGER PRIMARY KEY,
    origen      TEXT NOT NULL,                    -- bot | dashboard | scheduler | sistema
    tipo        TEXT NOT NULL,                    -- venta_creada, venta_aprobada,
                                                 -- escalado_timeout, trabajo_fallido, ...
    entidad     TEXT,                             -- nombre de la tabla afectada
    entidad_id  INTEGER,
    detalle_json TEXT,
    creado_en   TEXT DEFAULT (datetime('now'))
);
CREATE INDEX idxEventos_tipo ON eventos(tipo, creado_en);
```

## 3. Notas de implementación

- **WAL + un solo escritor:** todo `INSERT/UPDATE` sale de cuadra-core. El dashboard escribe únicamente vía la API REST del core (nunca abre la BD directamente).
- **`especificaciones_json`:** JSON1 extensiones de SQLite disponibles por defecto. Ejemplo de contenido:
  `{"cable_carro": "Sí, incluido", "garantía": "6 meses", "entrega_playa": "24h"}` — se enriquece automáticamente con cada respuesta de TCP y se inyecta en el contexto del LLM para no volver a preguntar.
- **Fotografía histórica en `ventas.contexto_json`:** al crear la venta se congela precio/listado del producto y zona del TCP del momento, porque el catálogo es editable y una auditoría no puede depender de datos que cambian.
- **Moneda explícita en todo:** ni precios ni comisiones sin `moneda`/`moneda_comision`. Conversiones (si algún día se necesitan) se hacen al registrar la venta, no al consultar.
- **Identidad de cliente:** la llave natural es `(canal, identificador)`. Un mismo cliente con Telegram y WhatsApp futuro son dos filas de `cliente_canales` apuntando al mismo `clientes.id` (el merge es manual/operativo, no automático).
- **Regla de seguridad de categorías (anti-baneo):** el scheduler solo publica un producto en grupos `ACTIVO` cuyas categorías (`grupo_categorias`) incluyan la `categoria_id` del producto. **Producto con `categoria_id = NULL` solo se publica en grupos que NO tengan categorías asignadas** (grupos libres). De este modo, un error de clasificación nunca puede mandar material de construcción al grupo de alimentos. La selección manual en `difusion_grupos` tiene prioridad sobre el auto-match, pero la regla de NULL se aplica igual.
- **Límite diario por grupo:** el scheduler consulta `envios_difusion` del día por grupo y respeta `max_posts_dia` + rango `[min_intervalo_min, max_intervalo_min]` de jitter. `estado = 'PROHIBIDO'` es una lista negra explícita (grupo donde ya pasó un problema).

## 4. Caminos de evolución (documentados, NO implementados)

| Necesidad futura | Cambio |
|---|---|
| Deduplicar "el mismo producto" en TCPs | Agregar `productos_base` + `ofertas(producto_base_id, tcp_id, …)`; los productos existentes pasan a ser ofertas |
| Liquidación de comisiones por período | Vista o tabla `liquidaciones(tcp_id, periodo, monto_total, pagado)` |
| Preferencias avanzadas de cliente | Ampliar `preferencias_json` (criterios de ranking: precio, zona, disponibilidad) |
| Operadores del dashboard | Tabla `usuarios` + login real (hoy: PIN compartido) |
| Historial de precios del producto | Tabla `precios_historial(producto_id, precio, moneda, fecha)` |
| Reglas avanzadas de publicación por grupo | Columnas en `grupos_difusion`: `precio_max`, `horario_inicio/fin`, `dias_permitidos` |
| Difusión multi-producto (catálogo semanal) | Tabla `difusion_productos(difusion_id, producto_id)` |
| Multi-instancia de canal (varios números WA) | Columna `canal_instancia` en `conversaciones` |
