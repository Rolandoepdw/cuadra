---
tipo: decisiones
proyecto: Cuadra — Gestor de Ventas con IA Local
estado: cerrado (fase de diseño)
actualizado: 2026-10-21
tags: [decisiones, constraints, stack, fases]
---

# 00 — Decisiones Cerradas del Debate de Diseño

Documento fuente de verdad. Todo lo que se decida posteriormente debe alinearse aquí o actualizar este archivo explícitamente.

## 1. Contexto y constraints del entorno

| Parámetro                      | Condición                                                                          |
| ------------------------------ | ---------------------------------------------------------------------------------- |
| Ubicación                      | Cuba — APIs de EE.UU. geobloqueadas; apagones y conexión intermitente              |
| Internet                       | **Hotspot de celular** (única fuente). Sin IP pública, sin dominio, sin TLS propio |
| Presupuesto                    | $0 USD — sin SaaS de pago                                                          |
| Hardware                       | Laptop con RTX 3050 Laptop (4 GB VRAM), 32 GB RAM                                  |
| RAM disponible para el sistema | **hasta ~20 GB** (tope, no objetivo — el backend apunta a <1 GB; el resto es para el modelo LLM, Whisper y trabajo personal) |
| SO de ejecución                | **Windows** — evitar Docker y WSL2 si es posible                                   |
| Usuarios concurrentes reales   | 2–4 (el sistema absorbe cientos, pero no es el objetivo)                           |

## 2. Stack definitivo (Fase 1)

| Componente | Elección | Nota |
|---|---|---|
| Orquestador | **Servicio único Python (FastAPI + asyncio)** | **n8n eliminado** — ahorra ~350 MB RAM, Postgres y workflows frágiles para máquina de estados |
| Canal Fase 1 | **Telegram** (aiogram, **polling**) | Polling evita webhooks/TLS/IP pública — encaja con hotspot |
| Canal Fase 2 | WhatsApp vía adaptador propio (evaluar Evolution API nativo vs whatsapp-web.js **sin Docker** en Windows) | Decisión pendiente al inicio de Fase 2 |
| LLM | **llama.cpp propio** (server gestionado por Cuadra) + `Bonsai-8B-Q1_0.gguf` (GPU) | Cuadra arranca/para su `llama-server` (concurrencia 1, `-ngl 99`, `-c 8192`); el modelo es seleccionable en caliente. Selección en `06_bakeoff_modelos.md` |
| STT | faster-whisper **`small` int8**, CPU, 4 hilos (default; validar con 10 audios reales) | Preserva VRAM para el LLM. `large` descartado en CPU (minutos por audio). `initial_prompt` con nombres del catálogo para mejorar marcas/números. Ver `01_arquitectura.md` §6 |
| Base de datos | SQLite (WAL, archivo único) | Un solo escritor (el servicio core) |
| Dashboard | **React + Vite + TypeScript** (build estático servido por FastAPI) | Bundle ~141 KB gzip (vs ~1.5-3 MB de Flutter Web); ecosistema shadcn/Tailwind v4; mismo autor-editor que el resto del stack web |
| Frontend extra | Ninguno | Sin Streamlit, sin Grafana |

**Microservicios: descartados.** Motivo: RAM, complejidad de debugging, SQLite no se comparte entre procesos, volumen real mínimo. Se adopta en su lugar **monolito modular con fronteras de dominio + API-first**; el server de `llama.cpp` (gestionado por Cuadra) y el gateway de WhatsApp ya son procesos externos de facto donde de verdad se necesita separación.

**Versiones mínimas:** Python 3.12+ (venv). Motor LLM: **llama.cpp propio** (build upstream, licencia MIT) instalado por `backend/scripts/install_llm.ps1` en `backend/tools/llamacpp/`, con el modelo GGUF ya descargado en `backend/models/`. **Ollama no se instala.** LM Studio solo para desarrollo. Node 20 LTS para el dashboard (React/Vite) y para el gateway WhatsApp de Fase 2.

## 3. Decisiones de negocio

- **Modelo:** comisionista multicanal. Cada venta genera comisión por producto/TCP.
- **Productos:** el producto es una **presentación abstracta del TCP**, no un catálogo canónico. Un mismo objeto comercial en dos TCPs son **dos productos independientes**. No hay tabla base de productos; el agente compara en tiempo real entre productos candidatos del catálogo global. Evolución documentada (no implementada): `base_products` + `offers` si algún día se quiere deduplicar.
- **Ventas:** el bot **puede crear** la venta a partir de la confirmación del TCP, pero queda en estado `DRAFT`/`PENDING_APPROVAL`. **El cierre del pago lo hace siempre el operador (dueño) desde el dashboard.** Flujo: `DRAFT → PENDING_APPROVAL → CONFIRMED → PAID` (+ `CANCELLED`).
- **Moneda:** **USD y CUP** — precio y comisión con moneda explícita por registro.
- **Identidad de cliente:** multi-canal desde el diseño (`customer_channels`: canal + identificador).

## 4. Disponibilidad y personalidad

- **El bot NO promete 24/7.** Se comporta como un vendedor humano experto: puede estar "atendiendo a otro cliente" o "verificando con el almacén". Offline = silencio natural, sin excusas de "sistema caído".
- Delays humanos: 1–4 s + simulación de "escribiendo…" antes de responder. Nunca respuesta instantánea.

## 5. Política anti-ban (máxima prioridad)

1. **Difusiones NO por WhatsApp** — para eso está Telegram. WhatsApp = solo 1:1 (inbound primero) y consultas al TCP. ~95% del tráfico WA es respuesta a inbound.
2. **Línea secundaria** dedicada al bot; si se banea, se rota. La línea principal nunca se toca.
3. **Calentamiento** de línea nueva: primera semana solo conversaciones reales, pocas salientes.
4. **Sesión estable:** una sola IP/dispositivo; no reescanear el QR seguido.
5. **Spintax** obligatorio en difusiones (no en 1:1). Intervalo configurable por grupo/estrategia en **segundos** (default 300–1200 s). Recurrencia: cada campaña se publica `repeat_passes` veces por grupo dentro de un ciclo de `repeat_window_days` días; al completar el ciclo se reinicia.
6. **Telegram (Telethon y Bot API):** `FloodWait`/`SlowModeWait` y `RetryAfter` se tratan como fallo transitorio con backoff (sin dormir al worker); tope configurable por grupo (default 20 posts/día, ajustable en Estrategia); respetar reglas de cada grupo. Aplica también a difusiones, no solo a 1:1.
7. Nada de links acortados; tono de marca (precio claro, sin spam) también es anti-ban.

## 6. Fases

| Fase | Alcance | Canal |
|---|---|---|
| **Fase 1** (piloto) | cuadra-core + persona Cuadra + catálogo + cola + STT + dashboard MVP + difusiones Telegram + **bucle de escalado TCP por Telegram** | Telegram (clientes y TCPs) |
| **Fase 2** (productivo) | Adaptador WhatsApp, migración de interacciones TCP/clientes a WA (85% del volumen real) | WhatsApp (cliente 85% / TCP 85%) |
| Fase 3+ | Liquidación de comisiones, deduplicación opcional de productos, interacción desde dashboard | — |

Los **adaptadores de canal** se diseñan desde Fase 1 con la interfaz pensada para que Fase 2 enchufe sin reescribir lógica de negocio.

## 7. Dashboard (Fase 1)

- **Alcance:** lectura completa + **CRUD de catálogo** (TCPs, productos, imágenes) + **CRUD de grupos de difusión y categorías** (curaduría 100% del operador, nunca del bot). Nada de operación manual de conversaciones.
- **Paneles:** General · Conversaciones · Catálogo · Grupos (difusión) · Difusiones · Estrategia · Auditoría · Salud.
- **Acceso:** solo LAN (red local del hotspot/laptop). **Con PIN.**
- **Futuro:** interacción (aprobar ventas, contestar manualmente) — ya previsto en el modelo de datos, no en Fase 1.
- **¿Web y no Desktop?** El dashboard es web: acceso desde móvil en la misma red sin instalar nada, y los estáticos los sirve el propio FastAPI (cero despliegue extra). Se eligió **React + Vite + TypeScript** (no Flutter): bundle ~141 KB gzip, ecosistema shadcn/Tailwind v4 y sin toolchain Flutter que mantener.

### 7.1 Gestión de grupos de difusión (curaduría del operador)

- El bot **no busca ni depura grupos** — eso es complejidad y riesgo de baneo injustificados en fases tempranas. La curaduría es 100% manual desde el dashboard.
- Cada **producto se clasifica en una categoría** (alimentos, tecnología, construcción, hogar…). Cada **grupo declara qué categorías acepta**.
- **Regla de seguridad anti-baneo:** un producto **sin categoría solo se publica en grupos sin restricción**. Un error de clasificación nunca puede mandar material de construcción a un grupo de alimentos.
- El scheduler cruza la categoría del producto de la campaña con los grupos activos, respeta tope de posts/día e intervalo por grupo, y aplica spintax. Todo queda logueado en `broadcast_posts` (auditable desde el panel).
- El registro de grupos es **canal-agnóstico** (telegram/whatsapp): el mismo panel servirá en Fase 2 sin cambios.
- **Los grupos no se eliminan:** igual que los TCP, se **desactivan** (`status = 'PAUSED'`) desde el botón de basura o desmarcando el check «Activo» del formulario. Desactivar conserva enlaces a campañas, posts e historial; `status = 'BLOCKED'` queda como lista negra explícita.

- **Identidad de envío por grupo (`broadcast_groups.sender`):** default `userbot` (Telethon); `bot` (Bot API) queda inactivo para difusiones y solo se conserva para el futuro. El userbot cubre grupos que rechazan bots. Es una cuenta normal: automatizarla viola los ToS de Telegram, así que riesgo de ban, `FloodWait` y reglas de cada grupo son parte del costo operativo. El userbot también **atiende los 1:1 que entran por esa cuenta**: `conversations.identity` registra por cuál cuenta entró cada chat y la respuesta sale por la misma (`bot` para los chats del bot, `userbot` para los del userbot), evitando respuestas duplicadas. `operator` no es identidad de difusión: es el humano que interviene en un chat 1:1.
- **Sin comunidades:** las comunidades de Telegram no son destinos publicables; no se modelan. Solo se registran grupos y canales.
- **Zonas del grupo (`zones` + `group_zones`):** catálogo de zonas con CRUD en el dashboard (Catálogo → Zonas) y relación M:N con los grupos, igual que las categorías. `suppliers.delivery_zone` (texto del TCP) sigue alimentando el token `{zona}`; el cruce de cobertura y el scoring multi-zona quedan para Fase 2.

### 7.2 Publicación: manual ahora, autopublicación después

- **Hito 7 = campaña manual:** el operador elige producto, plantilla y grupos. El scheduler solo decide **cuándo** publicar (elegibilidad, topes, intervalo, jitter); no elige el producto.
- **Autopublicación futura:** un planificador determinista (reglas + scoring) propondrá producto × grupo × franja. **El LLM no participa en la selección ni en los horarios**; solo conversa y, opcionalmente, redacta variantes.
- **Estrategia configurable:** el panel Estrategia edita tope diario e intervalos por defecto. Se aplican a grupos nuevos; con `apply_to_groups` se sobrescriben todos. Cada grupo puede seguir sobrescribiéndolos. Defaults provisionales: 20 posts/día, 5–20 min.
- **ID de producto en la publicación:** el texto incluye `ID: <products.id>` para que el cliente lo cite. Si la plantilla no usa `{id}`, el backend lo agrega al final. El pipeline resuelve un ID explícito (`ID 42`, `#42` o un número solo) **antes** de usar nombre, imagen o NLU. Si el ID no existe, no se reutiliza el producto del contexto.
- **Bot vs userbot vs humano:** `bot`/`userbot` son identidades de envío y de atención (`conversations.identity`); `operator` es autor humano. Un futuro takeover humano debe pausar la automatización de esa conversación, no cambiar el `sender` de difusiones.
- **Controles de publicación:** pausa global persistente (`broadcast.publishing`) y toggle por campaña (`broadcasts.enabled`), ambos reversibles. La pausa global detiene el scheduler y retira los posts `QUEUED`; «Enviar ahora» la ignora (acción consciente) y «Reevaluar» reanuda y reevalúa. Eliminar es destructivo; `CANCELLED` desaparece del modelo.

## 8. Restricciones de ingeniería

- Un solo proceso Python para lógica de negocio, colas, scheduler y API del dashboard.
- Dependencias mínimas del backend: `fastapi`, `uvicorn`, `aiogram`, `faster-whisper`, `httpx`, `APScheduler`, `telethon` (userbot) (+ `pillow` para imágenes).
- Dashboard (React/Vite) solo en `dashboard/`; el backend nunca depende de él en runtime (solo sirve su build estático).
- Preferir soluciones nativas de Windows sobre Docker/WSL2 en todo lo posible.
- **Ventana de contexto del LLM gestionada por el backend** (no "todo el historial"): últimos ~12 mensajes + resumen rodante + contexto estructurado del producto en JSON. Presupuesto objetivo ~2-2.5K tokens. Ver `01_arquitectura.md` §6.1.

## 9. Agente conversacional: decisiones de la prueba #2

- **Plantillas para pasos transaccionales; compositor LLM solo para preguntas abiertas.** Precio, stock, listados, cotización, captura de datos y confirmación salen de plantillas deterministas (rápidas y sin alucinación). Una pregunta abierta (`consulta_dato` sin dato del proveedor, queja, comparación, tema libre) la redacta el modelo con los datos reales del producto.
- **Guardrails en cada intento y fallback determinista.** Toda respuesta compuesta pasa por `guardrails.validate_reply`; si viola reglas se reintenta una vez con corrección y, si insiste o el modelo no está disponible, responde una plantilla. El cliente nunca queda en silencio.
- **Una venta abierta por producto y por conversación.** La etapa (`CAPTURE_DATA`, `CONFIRM`) describe el pedido, no el último mensaje: una pregunta no la resetea ni la cancela. La cantidad acordada se persiste con la venta (`sale_qty` + producto).
- **Un único trace por turno con `duration_ms`.** `pipeline.handle_turn` registra entrada y salida en una sola fila; la «latencia media» del dashboard es la del turno completo y la del LLM queda aparte (`avg_llm_ms`).
- **Trazas en archivo rotado y en SQLite.** `data/logs/traces.log` + tabla `traces`. Nivel `ALL` en desarrollo y `ERROR` en producción; PII enmascarada por defecto.
- **`cuadra.log` con señal, no ruido.** `apscheduler`, `httpx` y `telethon.network` quedan en `WARNING`: los ticks de 5 s ya no entierran los mensajes de negocio.
- **Retrieval híbrido operativo.** FTS5 + embeddings (`fastembed`, `paraphrase-multilingual-MiniLM-L12-v2`, 384 dims, modelo cacheado en `backend/models/fastembed`) fusionados con RRF y piso de similitud; si falta el modelo, degrada a FTS5 + fuzzy sin bloquear el arranque.

## 10. Protocolo de referencias y acks (prueba #3)

La prueba #3 cerró el ciclo completo (catálogo → escalación → venta → entrega → liquidación) pero dejó dos agujeros de trazabilidad: un `Entregado` se correlacionó con una escalación cualquiera y los acks se aplicaron «por parecido». Estas decisiones hacen que cada mensaje del TCP apunte a **una** entidad concreta.

- **ID primero, nunca ID único.** Todo mensaje que el bot manda al TCP (orden y consulta) incluye su referencia — `V-000001` para la venta, `[E-0003]` para la escalación — y pide responder citándola. Si el TCP la cita, la resolución es determinista. Si no la cita y hay **un único** candidato posible, se resuelve igual (no se le obliga a repetir por burocracia). Si hay **varios**, no se adivina: se le pide la referencia.
- **Un ack nunca es respuesta de producto.** `RECIBIDO` / `ENTREGADO` / `AGOTADO` sin venta que corresponda generan el evento `tcp_ack_unmatched` y se descartan; jamás caen en la correlación de escalaciones (fallo de la prueba #3, 04:57-04:59).
- **Cada ack mueve estados concretos.** `RECIBIDO` solo desde `DISPATCHED`; `ENTREGADO` desde `DISPATCHED` o `CONFIRMED`; `AGOTADO` desde ambos. Un ack que no encaja no reabre ni degrada la venta.
- **Ack repetido = `DUPLICATE`.** Se registra el evento y se ignora: ni mensaje al cliente ni `NEEDS_OPERATOR`. Un `ENTREGADO` repetido no puede volver a «entregar» una venta ya cerrada.
- **Erratas toleradas solo en mensajes de una palabra.** `ENTREGSDO`, `RECIVIDO` cuentan (≥ 85 % de similitud sobre palabras de 8+ letras); «la entrega es mañana» o «ENTREGA» no. La tolerancia existe porque el TCP escribe desde el móvil, no para adivinar frases.
- **Consulta ambigua no se resuelve por parecido.** Con varias escalaciones abiertas y sin `[E-####]`, no se resuelve ninguna: se encola `ask_tcp_reference` y se le pide la referencia al TCP, una sola vez por mensaje (evento `tcp_reference_asked`).
- **Al TCP siempre se le responde.** Sea ack ambiguo, repetido o sin venta, el bot contesta algo al TCP (pedir la referencia o ignorar en silencio el duplicado), nunca lo deja sin reacción ni lo usa como fuente de datos del producto.

## 11. Especificaciones y mensajes del TCP (prueba #3)

Dos reglas para que el bot no afirme lo que no sabe ni confunda al proveedor con el catálogo:

- **Un dato técnico solo se afirma si está en `specifications`.** Si el cliente pregunta por un tema del catálogo (procesador, tarjeta gráfica, memoria, almacenamiento, pantalla, batería, cámara, garantía) y el campo no existe, el bot **escala** en vez de componer: `pipeline._missing_spec_topics` lo detecta por léxico, sin depender del NLU. Si el dato existe, el compositor puede usarlo, pero `guardrails.check_specs` rechaza cualquier especificación que no esté en el catálogo (razón `spec`). Una negación honesta («no tengo ese dato del procesador») no se bloquea.
- **El texto del TCP que no es una respuesta no es un dato ni un mensaje para el cliente.** Una queja o negativa de proceso («no me has dado los datos de la venta», «no te puedo confirmar») se registra como `tcp_not_an_answer`: no se guarda en `specifications`, no se reenvía al cliente y la consulta sigue abierta. «No tengo unidades» sí es una respuesta válida. Si ninguna respuesta del TCP es utilizable, el cliente recibe `escalation_unconfirmed` (el dato sigue pendiente), nunca una disponibilidad inventada.

## 12. Escalaciones únicas y ofertas pendientes (prueba #3)

Dos fallos de continuidad: el TCP recibió la misma consulta dos veces (E-0001/E-0002) y el bot no recordaba qué había preguntado cuando el cliente contestaba «Si».

- **Una pregunta abierta por conversación, producto y texto.** `escalations.create` normaliza la pregunta (minúsculas, sin acentos ni puntuación) y reutiliza la escalación `PENDING` idéntica. Devuelve `(escalation, created)`; solo una fila nueva encola `send_escalation`, así el TCP no recibe la misma consulta dos veces. La reutilización se registra como `escalation_reused`. El dedupe no cruza a `ANSWERED`: una respuesta ya recibida no absorbe una pregunta nueva.
- **La oferta vive un solo turno.** `cuadra.ai.offers` guarda en el contexto la última pregunta que el bot hizo (`reserve`, `alternatives`, `siblings`) y la consume al turno siguiente. Una oferta nunca se puede responder dos turnos después ni sobrevive a un turno con otra pregunta.
- **Solo una respuesta desnuda ejecuta la oferta.** «Si», «dale», «ok», «No, gracias» se resuelven contra la oferta; «Si, ¿qué procesador tiene?» o «Vale, cuando me la traes» son turnos nuevos y siguen el flujo normal. `reserve` + «Si» inicia la venta (pide los datos que falten); `reserve` + «No» usa `sale_declined`; `alternatives`/`siblings` + «Si» muestra las alternativas reales.
- **La respuesta del almacén también deja oferta.** `tcp_triage.resume_escalations` guarda la oferta de reserva que ya incluye la plantilla `escalation_resolved_*` («¿Te reservo … y coordinamos la entrega?»), para que el siguiente «Si» del cliente cierre el ciclo en vez de volver a cotizar.

## 13. Logística, cobertura y post-venta (prueba #3)

El orden de decisión del turno es una garantía, no una preferencia: después de resolver la oferta pendiente y antes de la máquina de venta, el pipeline evalúa **post-venta → zona → logística**.

- **Post-venta (`DONE`) habla del pedido, no de una ficha nueva.** `pipeline._post_sale_outcome` responde con el estado real de la venta (`post_sale_pending`, `post_sale_confirmed`, `post_sale_delivered`) y cita siempre `V-000001`. No inventa precios ni plazos («48h») ni afirma que el almacén prepara lo que aún no confirmó. Una pregunta técnica real sigue su ruta de specs; una intención de compra nueva no se intercepta; una petición de listado («¿qué laptops tienes?») tampoco.
- **La zona se valida en el turno en que se menciona.** `pipeline._zone_coverage_outcome` usa `resolve_zone(..., active_only=False)`: una zona inactiva o no cubierta por el proveedor recibe `zone_not_covered` antes de pedir dirección y teléfono. Los nombres de 3 letras («UCI») requieren una pista en el mensaje («vivo en», «domicilio», «zona»).
- **La fecha de entrega no se escala antes del pedido.** `pipeline._logistics_outcome` responde `delivery_coordinated` mientras no haya una venta en `DISPATCHED` o posterior. Escalar una fecha cuando aún no existen zona, dirección y pedido es prometer lo que el TCP no puede cumplir.
- **La dirección conserva el número de casa.** `memory._complete_house_number` anexa el segmento `numero N` / `nro. N` / `#N` posterior a una coma cuando la dirección aún no lo trae. Sin esto, «calle 4 entre X y Y, numero 20» se guardaba sin el número.
- **Las salidas manuales del userbot son parte del historial.** `telegram_user.record_operator_message` persiste todo mensaje privado saliente en `messages` como `OUTBOUND` / `author='operator'`, con `external_id` de Telegram y evento `operator_message`. Los envíos programáticos se marcan antes de salir (`_mark_programmatic`) para no duplicarlos; escribir a un contacto sin conversación no crea un lead.
