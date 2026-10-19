---
tipo: plan
proyecto: Cuadra
estado: en ejecución (Hitos 1–4 hechos)
actualizado: 2026-10-19
tags: [fase1, plan, build, acceptance]
---

# 04 — Plan de Build Fase 1

> Entrada: `00_decisiones.md`, `01_arquitectura.md`, `02_modelo_datos.md`, `03_persona_prompts.md`.
> Objetivo: **piloto funcional por Telegram** con dashboard de observación + CRUD de catálogo.

## Orden de ejecución

### Hito 1 — Esqueleto del backend
1. `backend/` con `cuadra/` (paquete), `requirements.txt`, `data/` (gitignored).
2. `config.py` (rutas, PIN, modelo LLM, horario humano).
3. `db.py`: SQLite WAL + runner de migraciones (`migrations/001_init.sql` de `02_modelo_datos.md`).
4. `main.py`: FastAPI + lifespan que arranca (a) servidor uvicorn, (b) worker de cola, (c) scheduler, (d) polling de Telegram (con token opcional — sin token, modo dummy para desarrollo).

**Criterio de aceptación:** el proceso arranca, la BD se crea con todas las tablas, `GET /api/health` responde.

### Hito 2 — Dominio de catálogo + API de dashboard
5. `domain/catalog.py`: CRUD suppliers (TCP), productos e imágenes (mover archivo a `data/fotos/product_{id}/`).
6. `api/auth.py` (PIN), `api/models.py` + `api/routes_catalog.py` (catálogo) y `api/routes_dashboard.py` (salud).

**Criterio:** curl puede crear un TCP con 2 productos e imágenes; los datos quedan en SQLite.

### Hito 3 — Cola e IA
7. `ai/queue.py`: worker FIFO con prioridades sobre la tabla `jobs`; reintentos con backoff; supervivencia a reinicio.
8. `ai/stt.py`: faster-whisper (executor, 4 hilos).
9. `ai/llm.py` + `ai/llm_server.py`: cliente LLM (endpoint OpenAI-compatible del llama.cpp propio) + supervisor del server; NLU por JSON y respuesta por plantillas (`03_persona_prompts.md`). El pipeline vive en `ai/pipeline.py` y los handlers en `ai/handlers.py`.
10. Pipeline: mensaje entrante → (audio→transcripción) → intención → respuesta/envío/escalado.

**Criterio:** mediante el endpoint de debug `POST /api/debug/message` (solo activo con `CUADRA_DEBUG=1`) se simula un mensaje entrante y el sistema produce una respuesta coherente usando el catálogo; un audio de prueba se transcribe correctamente.

### Hito 4 — Canal Telegram + conversaciones ✅
11. `channels/base.py` (Protocol), `channels/__init__.py` (registro `get_adapter`) y `channels/telegram.py`: aiogram en polling que normaliza texto, voz/audio y foto a un evento interno vía `ai/ingest.py` (`ingest_incoming`).
12. `domain/conversations.py`: alta de cliente+canal, historial, contexto; persistencia de cada mensaje; `get_channel_identifier` para resolver el destino de salida.
13. `ai/handlers.py`: nuevo JOB `send_message` (typing + delay humano 1–4 s, 5–15 s fuera de horario, idempotente mediante el evento `message_sent`); `generate_reply` guarda el OUTBOUND y encola el envío.

**Criterio:** con un bot real de Telegram, un humano conversa sobre el catálogo y recibe respuestas con precio/moneda/zona correctos; todo queda en `messages`. *Verificado de punta a punta en modo dummy (entrada → respuesta → `send_message` con delay e idempotencia); pendiente la prueba con token real.*

### Hito 5 — Escalado al TCP (por Telegram, Fase 1)
14. `domain/escalations.py`: crea registro, escribe al TCP (`telegram_id` del proveedor), espera respuesta, enriquece `specifications_json`, retoma al cliente; timeout con 1 reintento.
15. Flujo inverso: la respuesta del TCP se correlaciona con el escalado pendiente más antiguo de ese TCP (y se marca).

**Criterio:** cliente pregunta dato faltante → TCP (otra cuenta de Telegram) responde → cliente recibe la respuesta y el producto queda enriquecido.

### Hito 6 — Dashboard Flutter Web
16. `dashboard/`: `flutter create` (web), cliente HTTP con `X-PIN`, navegación lateral con los 7 paneles.
17. Paneles: General, Conversaciones (lectura), Catálogo (CRUD completo), **Grupos** (CRUD grupos de difusión + categorías + asignación grupo↔categorías y producto↔categoría), Difusiones (con log de envíos por grupo), Auditoría, Salud.
18. Build web copiado a `backend/cuadra/web_dashboard/` y servido por FastAPI. (El mismo `lib/` servirá para `flutter build windows` si algún día se quiere el `.exe` local.)

**Criterio:** desde el móvil en la red local se ven datos reales en los 7 paneles, se edita el catálogo y se registra un grupo con sus categorías.

### Hito 7 — Grupos, categorías y difusiones Telegram
19. Migración de tablas `categories`, `broadcast_groups`, `group_categories`, `broadcasts`, `broadcast_group_links`, `broadcast_posts` (ya en `001_init.sql`) + `products.category_id`.
20. `domain/broadcasts.py`: plantilla con spintax, selección de destinos (auto-match por categoría, con prioridad a `broadcast_group_links` si hay selección manual; producto sin categoría → solo grupos libres), respeto de `max_posts_per_day` e intervalos, estados.
21. `scheduler/broadcasts.py`: jitter 3–40 min (o el rango del grupo), tope diario por grupo, log en `broadcast_posts`.
22. `Telethon` opcional (solo si se requieren userbot para grupos que el bot no puede alcanzar); empezar solo con Bot API donde alcance.

**Criterio:** registrando desde el dashboard dos grupos (uno con categoría "alimentos" y uno libre) y 3 productos (con y sin categoría), las difusiones llegan **solo a los grupos que corresponden**, con textos distintos (spintax) y horarios espaciados; el panel muestra el log de envíos por grupo.

### Hito 8 — Ventas (mínimo viable)
23. `domain/sales.py`: el bot crea `DRAFT → PENDING_APPROVAL` al confirmar TCP + cliente; endpoint para que el operador confirme/cancele/pague desde el dashboard (lectura + botón de aprobación).

**Criterio:** una venta nace de una conversación, aparece en el panel, y el operador la cierra manualmente.

## Criterios de aceptación globales de Fase 1

- [ ] Un cliente de Telegram compra un producto real usando solo al bot (consulta, precio con moneda, escalado si falta dato).
- [ ] Todo el historial es auditable en el panel (conversación completa, quién dijo qué, latencias).
- [ ] Reiniciar el laptop a mitad de conversación no pierde estado (la cola y conversaciones se reanudan); un trabajo `IN_PROGRESS` a media ejecución se reanuda **sin duplicar envíos** (verificar en panel Auditoría).
- [ ] El bot nunca responde en <1 s ni suena a contestadora rígida (auditar 20 conversaciones).
- [ ] Catálogo 100% administrable desde el dashboard sin tocar la BD.
- [ ] **Grupos 100% administrables desde el dashboard**; un producto sin categoría jamás llega a un grupo con restricciones (probar con caso negativo).
- [ ] **STT:** 10 notas de voz reales cubanas transcritas con `small` + `initial_prompt`; números y marcas correctos en ≥8/10 (si no, subir a `medium` o reforzar prompt).
- [ ] **Contexto:** 3 conversaciones largas (>20 mensajes) siguen coherentes gracias al resumen rodante (auditar en el panel).
- [ ] RAM total del proceso core < 1 GB con Whisper cargado (el server llama.cpp aparte).

## Fuera de alcance de Fase 1 (explícito)

- WhatsApp (cualquier forma) — Fase 2.
- Liquidación/estadísticas avanzadas de comisiones.
- Operación manual de conversaciones desde el dashboard.
- Login multiusuario, túnel de acceso remoto, HTTPS.
- Deduplicación de productos entre TCPs.

## Riesgos a vigilar durante el build

| Riesgo | Acción |
|---|---|
| El modelo suena muy robótico en cubano | Ajustar few-shots con conversaciones reales del panel; probar otros modelos del bake-off (`06_bakeoff_modelos.md`) — el modelo es seleccionable sin tocar código |
| Whisper CPU lento en audios largos | Limitar a 2 min; avisar al cliente "déjame escuchar eso" |
| Polling de Telegram latencia | Polling corto (1–2 s) es aceptable; webhook solo si algún día hay dominio |
| Imágenes de catálogo grandes | Comprimir a máx. 1280 px / ~500 KB al subir (el upload es por WiFi local — no consume datos del hotspot; el límite protege disco y los reenvíos por WA de Fase 2) |
