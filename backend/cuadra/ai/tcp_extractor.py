"""Structured extraction of commercial data from a TCP (supplier) message.

The model only *extracts* fields; the domain layer validates ranges, applies the
policy and writes SQL (doc 03 §4). No free-form reasoning is requested and the
output is a strict JSON Schema.

If the local model is down or answers invalid JSON twice, `extract` returns
`None`: the caller degrades to the legacy escalation behaviour instead of
blocking the TCP loop.
"""

import logging

from pydantic import BaseModel, Field, ValidationError

from cuadra.ai import llm

log = logging.getLogger("cuadra.tcp_extractor")

MAX_MESSAGE_CHARS = 2000

# Mirrors `TCPUpdateExtraction`. All fields are required (nullable) and no
# extra properties are allowed, per the project's structured-output contract.
EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "has_catalog_update": {"type": "boolean"},
        "stock": {"type": ["integer", "null"], "minimum": 0, "maximum": 100000},
        "price": {"type": ["number", "null"], "exclusiveMinimum": 0},
        # MLC is accepted here so the model can report it explicitly; the
        # domain layer never stores it (products.currency is USD/CUP only).
        "currency": {"type": ["string", "null"], "enum": ["USD", "CUP", "MLC", None]},
        "status": {
            "type": ["string", "null"],
            "enum": ["ACTIVE", "OUT_OF_STOCK", "PAUSED", None],
        },
        "new_specifications": {
            "type": "object",
            "additionalProperties": {"type": ["string", "number", "boolean"]},
        },
        "delivery_zone_names": {"type": "array", "items": {"type": "string"}},
        "customer_answer_summary": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "notes": {"type": "string"},
    },
    "required": [
        "has_catalog_update",
        "stock",
        "price",
        "currency",
        "status",
        "new_specifications",
        "delivery_zone_names",
        "customer_answer_summary",
        "confidence",
        "notes",
    ],
    "additionalProperties": False,
}


class TCPUpdateExtraction(BaseModel):
    has_catalog_update: bool
    stock: int | None = Field(default=None, ge=0, le=100_000)
    price: float | None = Field(default=None, gt=0)
    currency: str | None = None
    status: str | None = None
    new_specifications: dict[str, str | bool | int | float] = Field(
        default_factory=dict
    )
    delivery_zone_names: list[str] = Field(default_factory=list)
    customer_answer_summary: str = ""
    confidence: float = Field(ge=0.0, le=1.0)
    notes: str = ""


EXTRACTION_PROMPT = """\
Eres el auditor de catálogo de Cuadra. Extrae datos comerciales precisos de la
respuesta de un proveedor (TCP) en Cuba.

Reglas:
1. "fula", "USD", "dólares" → USD. "CUP", "moneda nacional", "pesos" → CUP.
   Si dice "MLC" devuélvelo como MLC (el sistema decidirá si es válido).
2. "no me queda", "se me fue", "agotado" → stock=0, status=OUT_OF_STOCK.
3. Si da un plazo de garantía, agrégalo en new_specifications["garantia"].
4. Si no estás seguro de un dato, déjalo en null y baja la confianza.
5. No inventes precios, stock ni zonas.
6. "has_catalog_update" es false cuando el mensaje no aporta ningún dato de
   catálogo (p. ej. solo un saludo o una pregunta).
7. customer_answer_summary: una o dos frases aptas para el cliente final que
   recojan TODO lo que el TCP respondió (accesorios, garantía, stock y precio).
   No omitas ningún dato ni resumas de más. Sin comisiones, costos ni datos
   internos. Vacía si no hay nada que contarle.
8. Responde solo con el JSON del esquema.
"""


def _product_block(product: dict) -> str:
    return (
        "Producto:\n"
        f"- ID: {product['id']}\n"
        f"- Nombre: {product['name']}\n"
        f"- Precio actual: {product['price']} {product['currency']}\n"
        f"- Stock físico: {product.get('stock')}\n"
        f"- Reservado: {product.get('stock_reserved')}\n"
        f"- Estado: {product.get('status')}\n"
        f"- Especificaciones: {product.get('specifications_json') or '{}'}\n"
    )


def build_messages(product: dict, raw_message: str) -> list[dict]:
    message = (raw_message or "").strip()
    if len(message) > MAX_MESSAGE_CHARS:
        message = message[:MAX_MESSAGE_CHARS] + "…"
    return [
        {"role": "system", "content": EXTRACTION_PROMPT},
        {
            "role": "user",
            "content": (
                f"{_product_block(product)}\n"
                f'Mensaje del TCP (texto o transcripción):\n"{message}"'
            ),
        },
    ]


async def extract(
    product: dict,
    raw_message: str,
    *,
    conversation_id: int | None = None,
    job_id: int | None = None,
) -> TCPUpdateExtraction | None:
    """One structured extraction with a single JSON correction retry.

    Returns `None` when the model is unavailable or keeps producing invalid
    output; callers must have a deterministic fallback.
    """
    messages = build_messages(product, raw_message)
    raw = await _chat(messages, conversation_id, job_id)
    if raw is None:
        return None
    try:
        return TCPUpdateExtraction.model_validate_json(raw)
    except ValidationError as first_error:
        log.warning("TCP extraction invalid, retrying once: %s", first_error)
        retry = [
            *messages,
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": (
                    "Tu respuesta anterior no validó contra el esquema JSON. "
                    f"Error: {first_error.errors(include_url=False)}. "
                    "Responde de nuevo SOLO con el JSON correcto."
                ),
            },
        ]
        raw_retry = await _chat(retry, conversation_id, job_id)
        if raw_retry is None:
            return None
        try:
            return TCPUpdateExtraction.model_validate_json(raw_retry)
        except ValidationError as second_error:
            log.warning("TCP extraction still invalid: %s", second_error)
            return None


async def _chat(
    messages: list[dict], conversation_id: int | None, job_id: int | None
) -> str | None:
    try:
        return await llm.chat(
            messages,
            schema=EXTRACTION_SCHEMA,
            schema_name="tcp_update_extraction",
            temperature=0.0,
            purpose="tcp_extraction",
            conversation_id=conversation_id,
            job_id=job_id,
        )
    except llm.LLMUnavailable as exc:
        log.warning("LLM unavailable for TCP extraction: %s", exc)
        return None
