Aquí tienes el archivo Markdown estructurado con formato nativo para **Obsidian** (incluyendo metadatos YAML, etiquetas, tablas, callouts y diagramas lógicos). 

Copia todo el contenido a continuación y guárdalo como `Contexto_Proyecto_Gestor_IA.md` dentro de tu Vault de Obsidian.

***

```markdown
---
tipo: especificacion_proyecto
proyecto: Gestor de Ventas Autónomo con IA Local
estado: fase_diseno
creado: 2026-10-18
tags:
  - cuba
  - ai-agents
  - local-llm
  - ollama
  - n8n
  - automation
  - whatsapp-bot
  - telegram-bot
---

# Proyecto: Agente Autónomo de Ventas Multicanal (Gestor TCP)

> [!ABSTRACT] Resumen Ejecutivo
> Sistema autónomo de corretaje comercial diseñado para operar en **Cuba** bajo restricciones severas de hardware, conectividad y presupuesto ($0 USD). Actúa como intermediario ("gestor de ventas") entre múltiples Trabajadores por Cuenta Propia (TCP/proveedores) y clientes finales a través de WhatsApp y Telegram, procesando texto, audios de voz e imágenes locales, con capacidad de consulta asíncrona a proveedores.

---

## 1. Contexto de Negocio y Modelo Operativo

* **Rol:** Gestor de ventas / Comisionista independiente multicanal.
* **Modelo de Monetización:** Comisión fija o porcentual por venta concretada (ejemplo: venta de producto de 250 USD $\rightarrow$ 20 USD de comisión pactada con el TCP).
* **Dinámica Multi-Proveedor (Multi-TCP):**
  * La plataforma representa el catálogo de varios TCPs simultáneamente.
  * Los clientes desconocen el contacto directo del TCP; el agente centraliza la atención.
  * Cada producto está mapeado a su respectivo TCP (contacto, zona de entrega, comisión, condiciones).

### 1.1 El "Bucle de Escalado al Proveedor" (Workflow Crítico)
1. **Consulta del Cliente:** Un cliente pregunta un dato no registrado en la base de datos (ej. *"¿El EcoFlow incluye cable para carro?"*).
2. **Pausa Asíncrona:** El agente indica al cliente que verificará con el almacén.
3. **Consulta al TCP:** El sistema envía automáticamente un mensaje por WhatsApp al TCP asignado al producto: *"Socio, tengo un cliente para el EcoFlow de 250 USD, ¿viene con cable de carro?"*.
4. **Respuesta y Aprendizaje:** El TCP responde. El sistema:
   * Procesa la respuesta.
   * Actualiza el catálogo local (SQLite) para no tener que volver a preguntar si otro cliente consulta lo mismo.
5. **Cierre con el Cliente:** El agente retoma la conversación con el cliente original y responde con el dato confirmado.

---

## 2. Restricciones del Entorno (Constraints Críticas)

> [!WARNING] Restricciones Obligatorias de Diseño
> Cualquier propuesta técnica posterior DEBE respetar estrictamente estos límites físicos y económicos.

| Parámetro | Límite / Condición | Implicación Técnica |
| :--- | :--- | :--- |
| **Ubicación** | Cuba | Bloqueo regional de APIs de EE.UU. (OpenAI, Anthropic, Google Cloud). Conexión inestable y apagones eléctricos. |
| **Ancho de Banda** | ~500 Kbps (intermitente) | El procesamiento debe ser **100% local**. Solo se usa internet para transportar texto plano o audios comprimidos. |
| **Presupuesto** | **$0 USD** | Prohibido el uso de APIs o servicios SaaS de pago (Meta Cloud API, Twilio, ManyChat, Make.com comercial). |
| **GPU / VRAM** | NVIDIA RTX 3050 Laptop (**4 GB VRAM**) | Solo modelos de lenguaje pequeños cuantizados (SLMs $\le$ 3B parámetros en Q4). |
| **Memoria RAM** | 32 GB totales (**Máx. 8-12 GB disponibles para el bot**) | El usuario necesita reservar entre 16 y 20 GB de RAM para trabajo personal continuo. No se pueden usar arquitecturas con microservicios pesados. |

---

## 3. Arquitectura Técnica Seleccionada (0 Coste, Ultra-Ligera)

```
[ Cliente (WA / TG) ]        [ TCP / Proveedor (WA) ]
         │                                │
         ▼                                ▼
 ┌────────────────────────────────────────────────────────┐
 │   Capa de Transporte (Evolution API + Telegram Bot)    │
 └───────────────────────┬────────────────────────────────┘
                         │ (Webhooks)
                         ▼
 ┌────────────────────────────────────────────────────────┐
 │   Orquestador Central: n8n (Self-hosted, ~350 MB RAM)  │
 ├────────────────────────────────────────────────────────┤
 │ • faster-whisper (CPU): Transcripción de audios        │
 │ • SQLite: Catálogo, estados de sesión y mapeo TCPs     │
 │ • Ollama (RTX 3050 - 4GB VRAM): Qwen 2.5 3B (LLM)      │
 │ • Almacenamiento Local: Fotos de productos en disco    │
 └────────────────────────────────────────────────────────┘
```

### 3.1 Componentes del Stack

1. **Orquestador y Motor de Estado:** `n8n` (Self-hosted mediante Docker o Node.js).
   * *Consumo:* ~350 MB RAM.
   * *Función:* Recibir webhooks, controlar las pausas asíncronas, consultar SQLite, disparar mensajes y despachar fotos.
2. **Motor de Inferencia LLM:** `Ollama` corriendo en la GPU.
   * *Modelo:* `qwen2.5:3b-instruct-q4_K_M` (o alternativamente `llama3.2:3b`).
   * *Uso de VRAM:* ~2.0 - 2.2 GB.
   * *Función:* Clasificación de intenciones, extracción de parámetros (JSON), generación de respuestas comerciales en español cubano formal/coloquial.
3. **Motor Speech-to-Text (Voz a Texto):** `faster-whisper`.
   * *Modelo:* `base` o `small`.
   * *Hardware:* **Ejecución estricta en CPU** (usando 4 hilos) para preservar el VRAM de la GPU para Ollama.
   * *Consumo:* ~500 MB RAM.
4. **Pasarela de WhatsApp:** `Evolution API` (Open Source / Docker).
   * *Conexión:* Instancia de WhatsApp Web vinculada vía código QR (usar SIM secundaria para evitar riesgos de baneo).
   * *Capacidades requeridas:* Recepción/envío de texto, audios de voz (PTT), imágenes con `caption` y manejo de grupos.
5. **Pasarela de Telegram:**
   * *Atención:* Telegram Bot API estándar (para clientes).
   * *Publicación en Grupos de Compraventa:* `Telethon` (Python Userbot) programado para difusión con intervalos aleatorios.
6. **Persistencia / Base de Datos:** `SQLite`.
   * Archivo único local, latencia < 2 ms, consumo de RAM casi nulo.

---

## 4. Modelo de Datos Preliminar (SQLite)

```sql
-- Tabla de Proveedores (TCPs)
CREATE TABLE tcps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre TEXT NOT NULL,
    telefono_wa TEXT NOT NULL, -- Número para consultas automáticas
    condiciones_pago TEXT,
    activo INTEGER DEFAULT 1
);

-- Tabla de Productos
CREATE TABLE productos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tcp_id INTEGER REFERENCES tcps(id),
    nombre TEXT NOT NULL,
    descripcion TEXT,
    precio_venta REAL NOT NULL,
    comision REAL NOT NULL,
    stock INTEGER DEFAULT 1,
    ruta_imagen_local TEXT, -- Ej: C:/fotos_catalogo/ecoflow.jpg
    especificaciones_json TEXT -- Memoria dinámica que se enriquece con las respuestas del TCP
);

-- Tabla de Sesiones y Esperas Asíncronas
CREATE TABLE conversaciones_pendientes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cliente_canal TEXT, -- 'whatsapp' | 'telegram'
    cliente_id TEXT,    -- Número o ChatID del cliente
    producto_id INTEGER REFERENCES productos(id),
    tcp_id INTEGER REFERENCES tcps(id),
    pregunta_cliente TEXT,
    estado TEXT DEFAULT 'ESPERANDO_TCP', -- 'ESPERANDO_TCP' | 'RESUELTO'
    fecha_creacion DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

---

## 5. Requisitos Funcionales Específicos

* [ ] **Atención Multimodal:** Si el cliente envía una nota de voz, el sistema debe transcribirla vía Whisper antes de consultar al LLM.
* [ ] **Despacho Gráfico (Imágenes + Texto):** Si el cliente pide fotos o detalles del producto, el bot debe enviar la imagen local adjunta con el texto generado por la IA en el pie de foto (*caption*).
* [ ] **Enriquecimiento del Catálogo:** Cualquier información nueva que provea el TCP durante una consulta debe agregarse al campo `especificaciones_json` del producto.
* [ ] **Tolerancia a Cortes de Red/Luz:** Los estados de las conversaciones deben persistir en SQLite para que el bot pueda reanudar pendientes al volver la corriente/conexión.
* [ ] **Prevención de Bloqueos:** Publicación en grupos de Telegram/WhatsApp regulada por temporizadores con variabilidad aleatoria (jitter de 3 a 7 minutos).

---

## 6. Descarte de Soluciones Inviables

* ❌ **Dify.ai:** Descartado por consumo excesivo de memoria (su arquitectura de microservicios consume >3 GB de RAM) y dificultad para manejar esperas asíncronas de terceros por WhatsApp.
* ❌ **APIs Cloud (OpenAI/Anthropic):** Descartadas por política de cero costes y geobloqueo en Cuba.
* ❌ **Automatización de Facebook Marketplace vía Web Scraping:** Descartada en fase inicial debido al riesgo inminente de baneo de cuentas de Meta. Se prioriza WhatsApp y Telegram.
```

***

### Instrucciones para el siguiente paso
Puedes suministrar este archivo a cualquier IA con el siguiente mensaje inicial:
> *"Lee el archivo `.md` adjunto que describe el contexto, las restricciones técnicas y el modelo de negocio de mi gestor de ventas con IA. A partir de esta especificación, comienza a generar la documentación de arquitectura detallada, los esquemas JSON de los flujos de n8n y los scripts necesarios para la fase 1."*