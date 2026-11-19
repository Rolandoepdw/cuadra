"""Shared constants for the turn modules (moved from pipeline.py)."""

import re

from cuadra.ai import guardrails


# Datos que solo el cliente puede proporcionar; nunca se escalan al TCP.
_DATO_CLIENTE = {
    "zona", "municipio", "dirección", "direccion", "domicilio", "barrio",
    "reparto", "teléfono", "telefono", "tel", "celular", "número",
    "numero", "contacto", "nombre", "destinatario", "a nombre de",
}

# F13 (prueba #4): no saludar en cada respuesta. El saludo se reserva para el
# inicio de la conversación o para cuando el cliente vuelve tras un hueco.
_GREETING_COOLDOWN_S = 600

_ID_PREFIXED = re.compile(
    r"\b(?:id|producto|codigo|código)\s*[:#]?\s*(\d{1,9})\b", re.IGNORECASE
)

_ID_BARE = re.compile(r"^\s*#?(\d{1,6})\s*$")

_COMPARE_SPLIT = re.compile(
    r"\s+(?:con|y|vs\.?|versus|contra|frente\s+a)\s+|[,;/\n]", re.IGNORECASE
)

_TOPIC_PRIORITY = {
    topic: index for index, topic in enumerate(guardrails.SPEC_TOPICS)
}

_CATEGORY_FILLER = {
    "de", "del", "la", "el", "los", "las", "un", "una", "unos", "unas", "que",
    "tienes", "tiene", "tienen", "hay", "tengo", "venden", "vende", "vendes",
    "me", "quiero", "queremos", "quisiera", "queria", "quería", "deseo",
    "busco", "buscamos", "buscando", "buscar", "ando", "andan", "andamos",
    "estoy", "estaba", "interesado", "interesada", "interesados", "interesadas",
    "interesa", "necesito", "necesitamos", "necesita", "algo", "por", "para",
    "en", "y", "o", "favor", "porfa", "ver", "mostrar", "muestrame",
    "muéstrame", "ensename", "enséñame", "dame", "lista", "listado", "catalogo",
    "catálogo", "precio", "precios", "cuanto", "cuánto", "cuesta", "cuestan",
    "vale", "valen", "disponible", "disponibles", "opciones", "opcion",
    "opción", "comprar", "compra", "cantidad", "stock", "hola", "buenas",
    "tardes", "dias", "días", "noches", "gracias", "mi", "mis", "su", "sus",
    "con", "sin", "sobre", "tambien", "también", "mas", "más", "otro", "otra",
    "otros", "otras", "tipo", "modelo", "modelos", "gama", "serie", "linea",
    "línea", "producto", "productos", "articulo", "artículo", "articulos",
    "artículos", "unidad", "unidades", "oferta", "ofertas", "promocion",
    "promoción", "promociones", "ocupo",
    # Confirmaciones: aceptan la pregunta anterior, no califican una familia.
    "si", "sí", "claro", "dale", "ok", "okay", "perfecto", "confirmo",
    "confirmado", "listo", "seguro", "obvio",
}

_AFFIRMATIVE = re.compile(
    r"\b(s[ií]|dale|ok|okay|confirmo|confirmado|claro|de acuerdo|perfecto|"
    r"vamos|lo quiero|lo llevo|res[eé]rvamelo|res[eé]rvalo|pa'?lante)\b",
    re.IGNORECASE,
)

_AFFIRMATIVE_START = re.compile(
    r"^[\s¡!¿?.,]*(s[ií]|dale|ok|okay|confirmo|confirmado|claro|de acuerdo|"
    r"perfecto|vamos|lo quiero|lo llevo|res[eé]rvamelo|res[eé]rvalo|pa'?lante)\b",
    re.IGNORECASE,
)

_NEGATIVE = re.compile(
    r"\b(no|todav[ií]a no|ahora no|d[eé]jalo|lo pienso|despu[eé]s)\b", re.IGNORECASE
)

# Prueba #2 (F1): `_NEGATIVE` busca "no" en cualquier parte, así que "quiero 3
# no 1" o "¿no tiene otro modelo?" cancelaban el pedido. Solo una respuesta que
# *empieza* rechazando cancela la venta.
_DECLINE = re.compile(
    r"^\s*(?:no|nop|nel|todav[ií]a no|ahora no|d[eé]jalo|lo pienso|despu[eé]s)\b",
    re.IGNORECASE,
)

_SALE_STAGES = ("CAPTURE_DATA", "CONFIRM")

# H-12: a message that is *only* a greeting must start the conversation again;
# answering it with the product of the previous turn makes the bot look deaf.
_GREETING = re.compile(
    r"^[\s¡!¿?.,]*(?:"
    r"(?:hola|buen[ao]s?(?:\s+(?:tardes|noches|d[ií]as))?|"
    r"bue(?:ns|nas|as)(?:\s+(?:tardes|noches|d[ií]as))?|"
    r"buenos\s+d[ií]as|qu[eé]\s+tal|saludos|hey|hi|hello)"
    r"[\s¡!¿?.,]*)+$",
    re.IGNORECASE,
)

# Below this the NLU is guessing (the unknown-product case often lands here), so
# the engine must not inherit the product of the previous turn (H-09).
LOW_CONFIDENCE = 0.35

# F4 (prueba #2): "¿y no tiene otro tipo de laptop?" pide alternativas, no un
# dato del proveedor. Prueba #5: "qué otras propiedades tiene" NO es una
# alternativa; por eso "otro/otra" ya no dispara solo.
_ALTERNATIVES = re.compile(
    r"\b(?:"
    r"otro\s+(?:modelo|tipo|producto|equipo|estilo)|"
    r"otra\s+(?:opci[oó]n|variante|versi[oó]n|marca|capacidad|medida|talla|"
    r"laptop|computadora|pc|tablet|moto|nevera|tel[eé]fono|celular)|"
    r"otros?\s+(?:modelos?|tipos?|productos?|equipos?)|"
    r"otras?\s+(?:opciones?|variantes?|marcas?)|"
    r"modelos?|similares?|opci[oó]n|opciones|variantes?|"
    r"diferentes?|algo\s+m[aá]s|"
    r"ning[uú]n\s+(?:tipo|modelo|otro)|"
    r"no\s+sea|"
    r"m[aá]s\s+(?:tipos?|modelos?|opciones?)"
    r")\b",
    re.IGNORECASE,
)

# Prueba #5: petición abierta de ficha ("qué propiedades tiene", "dime todo").
# Se responde con la descripción y las especificaciones reales, sin preguntar
# "¿qué dato necesitas?".
_OPEN_DETAILS = re.compile(
    r"\b(?:"
    r"propiedad(?:es)?|caracter[ií]stica(?:s)?|especificaci[oó]n(?:es)?|"
    r"specs?|detalles?|ficha(?:\s+t[eé]cnica)?|informaci[oó]n|"
    r"todo\s+lo\s+que\s+tengas|todos?\s+(?:los\s+)?(?:datos|detalles|que\s+tengas)|"
    r"todos?\s+los\s+que\s+tengas|"
    r"lo\s+que\s+tengas|dime\s+m[aá]s|cu[eé]ntame\s+m[aá]s|qu[eé]\s+m[aá]s|"
    r"qu[eé]\s+tiene|qu[eé]\s+trae|"
    r"^\s*todo\s*[.!?]*\s*$"
    r")",
    re.IGNORECASE,
)

# Etiquetas genéricas del NLU que no son un dato concreto del proveedor.
_GENERIC_DETAIL_LABELS = {
    "propiedad", "propiedades", "caracteristica", "caracteristicas",
    "especificacion", "especificaciones", "spec", "specs", "detalle",
    "detalles", "informacion", "ficha", "todo", "todos",
}

# Prueba #5: preguntar por la cobertura de entrega no es pedir un dato del
# producto ni una escalación. Se responde con las zonas reales del almacén.
_DELIVERY_COVERAGE = re.compile(
    r"\b(?:puedes?\s+entregar|me\s+puedes\s+entregar|entregan|hacen\s+entrega|"
    r"tienen\s+entrega|hay\s+entrega|cobertura|llegan\s+a|entrega\s+en|"
    r"zona\s+de\s+entrega|entregas\s+en)\b",
    re.IGNORECASE,
)

# Un listado de búsqueda no debe saturar el chat, pero el cliente necesita ver
# variedad antes de elegir: hasta 10 filas (y todas si hay menos de 5).
_TABLE_LIMIT = 10

# F3 (prueba #2): el cliente pide explícitamente el catálogo de una categoría
# ("dame un listado de laptops"), aunque el contexto tenga un producto abierto.
_LIST_REQUEST = re.compile(
    r"\b(?:listado|lista|cat[aá]logo|opciones|ofertas?|cu[aá]les|"
    r"mu[eé]strame|ens[eé]ñame|"
    r"qu[eé]\s+(?:\w+\s+){0,3}(?:tienes|hay|venden|manejas|manejan))\b",
    re.IGNORECASE,
)

# F14 (prueba #4): preguntas de cantidad. "cuánto cuesta" no cuenta como stock.
_STOCK_QUESTION = re.compile(
    r"\b(?:cu[aá]nt[ao]s?\s+(?:unidades|te\s+quedan|quedan|tienes|hay|"
    r"puedo|me\s+puedes|disponibles)|cantidad|stock|disponibles?|"
    r"te\s+quedan?|quedan?\s+unidades)\b",
    re.IGNORECASE,
)

# F12 (prueba #4): mensajes banales. Solo cuentan cuando TODO el mensaje es
# banal; "gracias, ¿y el precio?" sigue siendo una consulta.
_BANAL_WORDS = {
    "gracias", "grax", "muchas", "mil", "agradezco", "agradecido",
    "agradecida", "disculpa", "disculpas", "perdon", "perdone", "disculpe",
    "lamento", "siento", "lo", "ok", "okay", "okey", "vale", "de", "acuerdo",
    "perfecto", "listo", "entendido", "genial", "excelente", "bien", "bueno",
    "buena", "entonces", "pues", "ya", "claro", "no", "hay", "problema",
    "tranquilo", "tranquila", "nada", "igual", "saludos", "cordiales", "que",
    "tengas", "buen", "dia", "dias", "tarde", "tardes", "noche", "noches",
    "por", "todo", "muy", "amable", "atento", "atenta",
}

_BANAL_THANKS = {"gracias", "grax", "agradezco", "agradecido", "agradecida", "amable"}

_BANAL_SORRY = {
    "disculpa", "disculpas", "perdon", "perdone", "disculpe", "lamento", "siento",
}

_BANAL_ACK = {
    "ok", "okay", "okey", "vale", "acuerdo", "perfecto", "listo", "entendido",
    "genial", "excelente", "bien", "tranquilo", "tranquila", "nada", "problema",
}

# F6 (prueba #3): preguntas de logística. La fecha depende del pedido, así que
# antes de una venta despachada no se escalan ni se promete un plazo.
_LOGISTICS_QUESTION = re.compile(
    r"\b(?:fecha\s+de\s+entrega|d[ií]a\s+de\s+entrega|plazo\s+de\s+entrega|"
    r"tiempo\s+de\s+entrega|cu[aá]ndo\s+(?:llega|llegan|me\s+la\s+traes|"
    r"me\s+lo\s+traes|la\s+traes|lo\s+traes|me\s+llega|lo\s+recibo)|"
    r"cu[aá]nto\s+tarda|demora|hacen\s+env[ií]o|tienen\s+env[ií]o|"
    r"me\s+la\s+traes|me\s+lo\s+traes)\b",
    re.IGNORECASE,
)

# F8 (prueba #3): mensajes que en la etapa DONE se responden con el estado del
# pedido. Una pregunta técnica real sigue su flujo normal.
_POST_SALE_HINTS = re.compile(
    r"\b(?:gracias|vale|ok|okay|perfecto|listo|de\s+acuerdo|pedido|orden|"
    r"entreg\w*|lleg\w*|demora|tarda|cu[aá]ndo|d[oó]nde)\b",
    re.IGNORECASE,
)
