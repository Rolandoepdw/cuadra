"""Compatibility facade for the turn pipeline (see orchestrator.py)."""

import json
import logging

from cuadra.ai import memory

log = logging.getLogger("cuadra.pipeline")


async def handle_incoming(conn, conversation, inbound: dict) -> dict:
    """Backwards-compatible single-message entry point (legacy jobs/debug)."""
    turn = {
        "id": None,
        "conversation_id": conversation["id"],
        "last_message_id": inbound["id"],
        "message_ids_json": json.dumps([inbound["id"]]),
    }
    return await handle_turn(conn, conversation, turn)


async def maybe_summarize(conn, conversation) -> bool:
    """Backwards-compatible wrapper around the phase 2 memory module."""
    return await memory.maybe_summarize(conn, conversation)


# --- compatibility aliases (L3.0) -------------------------------
from cuadra.ai.turn.render import (  # noqa: F401
    _product_spec_corpus,
    _missing_spec_topics,
    _canonical_topic,
    _available_stock,
    _mark_greeting,
    _should_greet,
    _with_greeting,
    _availability_line,
    _render_product_card,
    _render_out_of_stock,
    _format_product_list,
    _line_topics,
    _detail_entries,
    _entry_priority,
    _render_details,
    _table_cell,
    _ellipsis,
    _product_highlights,
    _format_product_table,
    _render_catalog_table,
    _render_siblings,
    _category_listing,
    _is_generic_detail_label,
)
from cuadra.ai.turn.references import (  # noqa: F401
    product_id_from_text,
    _retrieve_candidates,
    _lexically_supported,
    _find_product_in_text,
    _resolve_product,
)
from cuadra.ai.turn.guards import (  # noqa: F401
    _guard_product,
    _guard_context,
    _log_guardrail,
    _safe_template,
)
from cuadra.ai.turn.composer import (  # noqa: F401
    _build_base_messages,
    _fast_template,
    _compose_open,
)
from cuadra.ai.turn.policy import (  # noqa: F401
    _dato_pertenece_a,
    _dedupe,
    _comparison_products,
    _category_request,
    _single_category_product,
    _stock_outcome,
    _is_greeting,
    _banal_kind,
    _asks_for_alternatives,
    _asks_for_open_details,
    _catalog_request,
    _asked_detail_topics,
    _confirms,
    _is_decline,
    _stage_for,
    _decide,
)
from cuadra.ai.turn.sales_flow import (  # noqa: F401
    _sale_quantity,
    _sale_slots,
    _missing_slots,
    _sale_guard,
    _sale_stock_outcome,
    _sale_failure,
    _sale_product,
    _delivery_zones_outcome,
    _zone_coverage_outcome,
    _post_sale_outcome,
    _banal_outcome,
    _logistics_outcome,
    _offer_product,
    _offered_alternatives_outcome,
    _maybe_handle_sale,
)
from cuadra.ai.orchestrator import (  # noqa: F401
    _turn_messages,
    _turn_text,
    _classify,
    _classify_light,
    handle_turn,
    _run_turn,
)
from cuadra.ai.turn.frame import (  # noqa: F401
    _update_context,
)
