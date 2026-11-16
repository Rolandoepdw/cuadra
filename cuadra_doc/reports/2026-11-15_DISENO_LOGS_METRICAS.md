# DISEÑO: Sistema de logs y métricas de calidad para Cuadra

**Estado:** Borrador para debate. No implementado todavía.
**Objetivo:** Definir qué logs emitir, qué métricas calcular y cómo evaluar la calidad del agente sin depender de que el operador tome notas manuales.

---

## 1. Principio rector

> "Si no se mide, no se puede mejorar."

El sistema actual solo guarda eventos en tabla `events` con `details_json` no estructurado. Esto obliga a hacer `grep` y `SELECT` ad hoc cada vez que se quiere saber algo. El nuevo sistema debe permitir responder en segundos preguntas como:

- ¿Cuánto tardó de media un cliente en recibir respuesta?
- ¿Cuántas veces se repitió la misma plantilla en una conversación?
- ¿Cuántas ventas se quedaron atascadas en DISPATCHED?
- ¿Cuántas escalaciones respondió mal el TCP?
- ¿Qué % de mensajes del cliente no tuvieron respuesta?

---

## 2. Arquitectura propuesta

### 2.1 Tabla `turn_metrics` (nueva)

Una fila por turno procesado, con todo el desglose de latencia y resultado:

```sql
CREATE TABLE turn_metrics (
    id INTEGER PRIMARY KEY,
    turn_id INTEGER NOT NULL,
    conversation_id INTEGER NOT NULL,
    
    -- Tiempos (ms)
    debounce_ms INTEGER,      -- espera por silencio del cliente
    queue_ms INTEGER,         -- espera en la cola de jobs
    nlu_ms INTEGER,           -- clasificación de intención
    compose_ms INTEGER,       -- generación de respuesta (0 si template)
    send_wait_ms INTEGER,     -- espera por token bucket / chat floor
    send_ms INTEGER,          -- envío real por Telegram
    total_ms INTEGER,         -- suma de todo
    
    -- Resultado
    intent TEXT,              -- consulta_precio, consulta_stock, comprar, etc.
    generator TEXT,           -- template, llm, fallback
    template_name TEXT,       -- nombre de la plantilla usada si aplica
    template_variant INTEGER, -- índice de variante (0, 1, 2...)
    
    -- Estado de venta
    sale_id INTEGER,
    sale_status TEXT,         -- PENDING_DISPATCH, DISPATCHED, CONFIRMED, etc.
    quantity_requested INTEGER,-- lo que pidió el cliente (del NLU)
    quantity_created INTEGER,  -- lo que se creó realmente
    
    -- Escalación
    escalation_id INTEGER,
    escalation_status TEXT,   -- PENDING, RESOLVED, TIMEOUT
    
    -- Calidad
    is_repetition INTEGER,    -- 1 si la plantilla es idéntica a la anterior del turno
    customer_frustrated INTEGER, -- 1 si hay indicios de enfado (heurístico)
    
    created_at TEXT NOT NULL
);

CREATE INDEX idx_turn_metrics_conv ON turn_metrics(conversation_id);
CREATE INDEX idx_turn_metrics_created ON turn_metrics(created_at);
CREATE INDEX idx_turn_metrics_intent ON turn_metrics(intent);
```

### 2.2 Tabla `message_quality` (nueva)

Una fila por mensaje del cliente, con su clasificación y qué respondió el bot:

```sql
CREATE TABLE message_quality (
    id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL,
    conversation_id INTEGER NOT NULL,
    
    -- Clasificación del mensaje del cliente
    intent TEXT,
    confidence REAL,
    is_greeting INTEGER,
    is_complaint INTEGER,
    is_negation INTEGER,      -- "no", "no quiero", etc.
    quantity_requested INTEGER,
    
    -- Qué respondió el bot
    reply_message_id INTEGER,
    reply_generator TEXT,
    reply_latency_ms INTEGER,
    
    -- Resultado
    sale_created INTEGER,     -- 1 si esta mensaje provocó una venta
    escalation_created INTEGER,
    
    created_at TEXT NOT NULL
);
```

### 2.3 Tabla `sale_flow` (nueva)

Trace completo de cada venta desde creación hasta entrega:

```sql
CREATE TABLE sale_flow (
    id INTEGER PRIMARY KEY,
    sale_id INTEGER NOT NULL,
    
    -- Estados y tiempos
    created_at TEXT,
    dispatched_at TEXT,
    confirmed_at TEXT,
    delivered_at TEXT,
    settled_at TEXT,
    
    -- Acks del TCP
    ack_received_at TEXT,
    ack_action TEXT,          -- CONFIRMED, DELIVERED, NEEDS_OPERATOR, AMBIGUOUS
    ack_message_id INTEGER,   -- id del mensaje del TCP
    ack_was_erroneous INTEGER,-- 1 si se aplicó a venta equivocada antes de corregir
    
    -- Cliente
    conversation_id INTEGER,
    customer_id INTEGER,
    
    -- Datos de la venta
    product_id INTEGER,
    quantity INTEGER,
    amount REAL,
    zone TEXT,
    
    created_at TEXT NOT NULL
);
```

---

## 3. Eventos de evento (en tabla `events` existente)

Añadir nuevos `type` a `events` para marcar hitos sin crear tablas nuevas:

| Tipo | Cuándo se dispara | Qué lleva en `details_json` |
|------|------------------|-----------------------------|
| `turn_started` | Cuando se empieza a procesar un turno | `turn_id`, `message_count`, `debounce_due_at` |
| `nlu_classified` | Después del classify | `intent`, `confianza`, `cantidad`, `producto`, `latency_ms` |
| `template_selected` | Cuando se elige una plantilla | `template_name`, `variant_index`, `is_repetition` |
| `sale_created` | Venta creada | `sale_id`, `order_code`, `quantity`, `amount`, `zone` |
| `sale_quantity_updated` | Cantidad actualizada | `sale_id`, `old_qty`, `new_qty` |
| `escalation_created` | Escalación al TCP | `escalation_id`, `product_id`, `supplier_id`, `question` |
| `escalation_answered` | TCP responde | `escalation_id`, `tcp_answer`, `latency_ms` |
| `tcp_ack_applied` | ACK del TCP aplicado | `sale_id`, `order_code`, `action`, `was_ambiguous` |
| `guardrail_violation` | Respuesta bloqueada | `reason` (format, length, price, placeholder...) |
| `customer_frustrated` | Heurística de enfado | `trigger` (repetición, negación, insulto...), `conversation_id` |
| `rate_limited` | Token bucket vacío | `sender`, `wait_ms`, `tokens_available` |

---

## 4. Métricas de calidad (calculadas desde las tablas)

### 4.1 Latencia

| Métrica | Fórmula | Objetivo |
|---------|---------|----------|
| p50 inbound→outbound | percentil 50 de `total_ms` en `turn_metrics` | < 5s |
| p95 inbound→outbound | percentil 95 de `total_ms` | < 12s |
| p50 NLU | percentil 50 de `nlu_ms` | < 2s |
| p50 compose | percentil 50 de `compose_ms` (solo LLM) | < 3s |
| Tiempo de debounce medio | AVG(`debounce_ms`) | ~0.8s |

### 4.2 Repetición (calidad de conversación)

| Métrica | Fórmula | Objetivo |
|---------|---------|----------|
| Templates repetidos consecutivos | COUNT donde `is_repetition=1` / total turnos | < 5% |
| Plantilla única más usada | TOP 1 de `template_name` por conversación | < 70% de los turnos de una misma conv |
| Turnos sin venta (cliente pidió comprar) | Turnos con `intent=comprar` y `sale_id IS NULL` | 0 (o justificado) |

### 4.3 Ventas

| Métrica | Fórmula | Objetivo |
|---------|---------|----------|
| Ventas creadas por TCP | COUNT(`sales`) agrupado por `supplier_id` | > 0 por TCP activo |
| Ventas atascadas DISPATCHED | COUNT donde `status='DISPATCHED'` y `dispatched_at` > 30 min | 0 |
| Acks aplicados correctamente | COUNT(`ack_was_erroneous=0`) / total acks | 100% |
| Cantidad cumplida | AVG(`quantity_created / quantity_requested`) | ~1.0 |

### 4.4 Escalaciones

| Métrica | Fórmula | Objetivo |
|---------|---------|----------|
| Escalaciones creadas | COUNT(`escalations`) | Solo datos proveedor, nunca cliente |
| Escalaciones respondidas | COUNT donde `tcp_answer IS NOT NULL` | > 90% |
| Tiempo de respuesta TCP | AVG(`answered_at - created_at`) | < 10 min |
| Escalaciones innecesarias | COUNT donde `datos_faltantes` son del cliente | 0 |

### 4.5 TCP

| Métrica | Fórmula | Objetivo |
|---------|---------|----------|
| TCP sin respuesta | COUNT mensajes TCP sin evento de respuesta | 0 |
| Acks por TCP | COUNT `tcp_ack_applied` por `supplier_id` | > 0 si hay ventas DISPATCHED |
| Acks ambiguos | COUNT donde `action=AMBIGUOUS` | 0 (o revisados por operador) |

---

## 5. Consultas SQL de ejemplo (para el dashboard o análisis)

```sql
-- 1. Latencia por hora del día
SELECT
    strftime('%H', created_at) AS hora,
    COUNT(*) AS n,
    ROUND(AVG(total_ms), 1) AS avg_ms,
    ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY total_ms), 1) AS p50_ms,
    ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY total_ms), 1) AS p95_ms
FROM turn_metrics
GROUP BY hora
ORDER BY hora;

-- 2. Repetición de plantillas por conversación
SELECT
    conversation_id,
    template_name,
    COUNT(*) AS veces_usada,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (PARTITION BY conversation_id), 1) AS pct
FROM turn_metrics
WHERE generator = 'template'
GROUP BY conversation_id, template_name
HAVING COUNT(*) > 1
ORDER BY pct DESC;

-- 3. Ventas atascadas en DISPATCHED
SELECT
    s.id, s.order_code, s.product_id, s.supplier_id,
    s.dispatched_at,
    (julianday('now') - julianday(s.dispatched_at)) * 24 * 60 AS minutos_estancado
FROM sales s
WHERE s.status = 'DISPATCHED'
  AND (julianday('now') - julianday(s.dispatched_at)) * 60 > 30;

-- 4. Escalaciones innecesarias (datos del cliente)
SELECT e.id, e.question, c.name
FROM escalations e
JOIN conversations c ON c.id = e.conversation_id
WHERE e.question LIKE '%zona%' OR e.question LIKE '%dirección%' OR e.question LIKE '%teléfono%';
```

---

## 6. ¿Qué NO se debe loggear (privacidad y secretos)

| Dato | Por qué no |
|------|------------|
| Teléfono completo del cliente | Dato sensible, usar `customer_phone` con máscara |
| Dirección completa | Dato sensible, usar zona + primera calle solo |
| Precios internos/comisión | Nunca salir de la base de datos, usar `check_prices` |
| Prompt completo del sistema | Información interna del negocio |
| Texto completo del cliente si contiene PII | Hash o truncar |

---

## 7. Decisiones abiertas para debatir

1. **¿Dónde se calculan las métricas?** ¿En el código Python (al finalizar cada turno) o en consultas SQL periódicas sobre `events`?
   - Opción A: Python escribe directamente a `turn_metrics` y `sale_flow` (más rápido, más acoplado)
   - Opción B: Todo se deja en `events` y se calcula con SQL después (más desacoplado, más lento para consultar)

2. **¿Se expone un endpoint `/api/metrics` en el dashboard para ver estos datos en tiempo real?**
   - Sí: permite al operador ver calidad mientras prueba
   - No: solo SQL directo, más simple

3. **¿Hay umbrales de alerta automática?** (ej. "si p95 > 15s por 10 minutos, enviar alerta")
   - Sí: requiere un servicio de monitoreo adicional
   - No: solo se consulta bajo demanda

4. **¿Se guarda el texto completo del cliente en `message_quality`?**
   - Sí: permite analizar por qué el NLU falló
   - No: solo intenciones y entidades, más privado

5. **¿Qué considerar "cliente frustrado"?** ¿Solo repetición de plantilla, o también palabras clave (insultos, "no quiero", "está lento")?

---

## 8. Próximo paso tras debate

Una vez acordadas las decisiones de la sección 7, implementar:
1. Migración SQL para las 3 tablas nuevas
2. Inserción de métricas en `handle_turn()` y `send_message()`
3. Consultas de ejemplo en el dashboard o script de análisis
4. Umbrales de alerta (si se decide)

---

*Fin del diseño de logs.*
