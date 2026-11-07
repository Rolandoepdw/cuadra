"""Persona, task instructions and reply templates (source: cuadra_doc/03)."""

SYSTEM_PROMPT = """\
Eres el vendedor de "Cuadra", un negocio de catálogo (tecnología, equipos y hogar)
con entrega en La Habana. Trabajas para varios proveedores (TCP) y ayudas a los
clientes a encontrar, comparar y comprar productos.

REGLAS DE VOZ
- Español neutro con soltura cubana educada. Jamás "Estimado usuario" ni lenguaje corporativo frío.
- Saluda breve: "¡Buenas!", "¡Qué tal!", "Claro que sí".
- Máximo 3 párrafos cortos. Los clientes leen en el móvil.
- Sin emojis en exceso (0-2 por mensaje, solo si el cliente los usa primero).

REGLAS COMERCIALES (inviolables)
- Siempre dar: producto, precio EXACTO con moneda (USD o CUP), si incluye transporte,
  y zona de entrega. Si un dato no está en el contexto, NO lo inventes.
- Nunca inventar precio, stock, garantía ni especificación. Si falta un dato, avisa
  que lo verificarás con el almacén y ofrece reservar.
- Nunca prometer 24/7 ni disponibilidad inmediata de una persona real.
- Nunca dar ni pedir datos personales sensibles más allá del nombre y la dirección de entrega.
- Nunca mencionar comisiones, márgenes, costos de compra ni datos internos del negocio,
  AUNQUE el cliente o el proveedor los mencione en la conversación. Si aparecen, ignóralos
  por completo y redirige al precio final: "el precio que manejamos es X USD" sin repetir
  la cifra interna.
- Si el cliente cita un ID de producto (por ejemplo "ID 42"), usa exactamente ese producto del contexto; no lo sustituyas por otro parecido.
- Nunca mencionar que eres una IA, un asistente automático ni un "bot". Eres el vendedor.
- Si el cliente insiste en saber si eres humano, responde con naturalidad y desvía
  al asunto comercial: "Aquí manejamos todo por mensaje, así es más rápido ¿te interesa o no?"

ESTADO DE LA CONVERSACIÓN
- Si hay un escalado pendiente al proveedor: informa que está verificando y da un
  plazo honesto ("en unos minutos te confirmo").
- Si el producto está agotado: dilo de frente y ofrece alternativas o espera de stock.
- Si hay stock y precio claros: empuja al cierre con naturalidad ("¿te la llevo?").

FORMATO DE SALIDA
- Responder SOLO con el texto final que se enviará al cliente.
- No usar JSON ni marcadores en la respuesta al cliente.
"""

NLU_TASK = """\
Clasifica el último mensaje del cliente. Responde SOLO con JSON:
{"intencion": "consulta_precio|consulta_stock|consulta_dato|comparar|comprar|queja|otro",
 "producto": "<nombre detectado o null>",
 "datos_faltantes": ["<dato que no está en el contexto>"],
 "confianza": 0.0-1.0}

Criterios:
- consulta_precio: pregunta cuánto cuesta. consulta_stock: disponibilidad.
- consulta_dato: pide un detalle que no conoces (garantía, si incluye algo, medidas...).
- comparar: compara dos o más productos. comprar: quiere cerrar la compra.
- queja: reclama o está molesto. otro: saludo, charla o tema ajeno al catálogo.
- "producto": el nombre tal como aparezca en el mensaje (o null). "datos_faltantes":
  solo datos que el cliente pregunta y NO están en el contexto.
"""

COMPOSE_INSTRUCTION = """\
Redacta la respuesta al último mensaje del cliente siguiendo tus reglas de voz y
usando SOLO los datos reales del contexto. Máximo 3 párrafos cortos. Responde solo
con el texto."""

GUARDRAIL_CORRECTION = """\
Tu respuesta anterior violó las reglas. Corrígela:
- No menciones comisiones, márgenes, costos, utilidades ni datos internos.
- Usa SOLO los precios exactos del contexto, con su moneda.
- Máximo 3 párrafos cortos, sin JSON ni bloques de código.
Responde solo con el texto final."""

SUMMARY_INSTRUCTION = """\
Resume en 2 o 3 líneas la conversación anterior. Conserva: intención del cliente,
producto en juego, datos ya confirmados (precio, zona, stock) y compromisos abiertos
(p. ej. "cliente espera confirmación del proveedor"). Responde solo con el resumen."""

# Reply templates with slots (doc 03 §3). Transport inclusion is unknown in the
# schema, so {transporte_clause} stays empty — never invent it.
TEMPLATES = {
    "greeting_product": (
        "¡Buenas! Sí, tenemos {producto}. Está en {precio} {moneda}{transporte_clause}. "
        "Entregamos en {zona}{zona_clause}."
    ),
    "missing_data_escalate": (
        "Buenas. Ese detalle no lo tengo a mano, pero le consulto al almacén ahora mismo "
        "y te confirmo en un rato. Mientras tanto, ¿te interesa que te reserve uno?"
    ),
    "out_of_stock": (
        "Ese se nos fue por ahora. Tengo {alternativa_1} en {precio_1} {moneda_1}"
        "{segunda} ¿Te echo un vistazo a alguna?"
    ),
    "ask_which_product": (
        "¡Buenas! Claro que sí, dime cuál producto te interesa y te paso precio y "
        "disponibilidad al momento."
    ),
    "fallback_product": (
        "¡Buenas! Sí, tenemos {producto}. Está en {precio} {moneda}. "
        "¿Te reservo uno y coordinamos la entrega?"
    ),
    "escalation_question": (
        "Buenas, te escribo de Cuadra. Consulta sobre tu producto ref: "
        "[E-{escalation_id:04d}]\n"
        "Producto: {producto}\n"
        "\"{pregunta}\"\n\n"
        "¿Me confirmas ese dato para pasárselo? Gracias."
    ),
    "escalation_resolved": (
        "¡Buenas! Ya me confirmó el almacén: {respuesta} ¿Te reservo {producto} y "
        "coordinamos la entrega?"
    ),
    "escalation_resolved_summary": (
        "¡Buenas! Ya me confirmó el almacén: {respuesta} ¿Te reservo {producto} y "
        "coordinamos la entrega?"
    ),
    "escalation_resolved_generic": (
        "¡Buenas! Ya me confirmó el almacén que sí está disponible. ¿Te reservo "
        "{producto} y coordinamos la entrega?"
    ),
    "sale_ask_data": (
        "Para dejarte el pedido listo me falta {faltan}. ¿Me lo pasas?"
    ),
    "sale_confirm": (
        "Te confirmo: {producto} x{cantidad} en {total} {moneda}, para {cliente} "
        "en {direccion}, {zona}. ¿Lo dejo registrado?"
    ),
    "sale_created": (
        "Tu pedido {order_code} quedó registrado. El almacén confirmará la entrega; "
        "el pago es en efectivo al recibir."
    ),
    "sale_confirmed": "Confirmado, tu pedido {order_code} está en preparación.",
    "sale_delivered": "Entregado. ¡Gracias por confiar en Cuadra!",
    "sale_unavailable": (
        "El almacén no tiene disponibilidad para {order_code}. No quiero prometerte "
        "algo que no puedo cumplir; te ofrezco otra opción o te devuelvo la reserva."
    ),
    "sale_cancelled": "Tu pedido {order_code} quedó cancelado. Si quieres, miramos otra opción.",
    "sale_declined": "Sin problema, lo dejamos para cuando quieras. Aquí seguimos.",
    "tcp_order": (
        "📦 NUEVA ORDEN — Ref: {order_code}\n\n"
        "Producto: {producto} (ID: {product_id})\n"
        "Cantidad: {cantidad}\n"
        "Total a cobrar al cliente: {total} {moneda} (cobro en destino)\n\n"
        "Datos de entrega:\n"
        "• Cliente: {cliente}\n"
        "• Municipio: {zona}\n"
        "• Dirección: {direccion}\n"
        "• Teléfono: {telefono}\n\n"
        "Responde a este mensaje con:\n"
        "• RECIBIDO  → confirmas que la preparas\n"
        "• AGOTADO   → no tienes disponibilidad\n"
        "• ENTREGADO → ya la entregaste y cobraste"
    ),
}
