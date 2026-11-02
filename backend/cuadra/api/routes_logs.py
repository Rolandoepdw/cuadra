"""Audit log viewer (`events` table) grouped into operational categories.

The dashboard renders exactly what this module returns: a short Spanish label,
a humanized one-line summary, a severity and the raw details for the expandable
view. Keeping the presentation here avoids duplicating event knowledge in the
frontend when new event types are added.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status

from cuadra import db
from cuadra.api.auth import require_pin
from cuadra.domain import events

router = APIRouter(prefix="/api", dependencies=[Depends(require_pin)])

WINDOWS: dict[str, int | None] = {"1h": 1, "24h": 24, "7d": 168, "all": None}

CATEGORY_LABELS = {
    "publications": "Publicaciones",
    "conversations": "Conversaciones",
    "entities": "Cambios",
    "system": "Sistema",
}

LABELS = {
    # publications
    "broadcast_scheduled": "Publicación programada",
    "broadcast_posted": "Publicación enviada",
    "broadcast_post_failed": "Publicación fallida",
    "broadcast_no_targets": "Campaña sin destinos",
    "broadcast_cycle_complete": "Ciclo de campaña completado",
    "broadcast_send_now": "Envío inmediato solicitado",
    "broadcasts_reevaluated": "Campañas reevaluadas",
    "broadcast_publishing_paused": "Publicaciones pausadas",
    "broadcast_publishing_resumed": "Publicaciones reanudadas",
    "broadcast_enabled": "Campaña habilitada",
    "broadcast_disabled": "Campaña deshabilitada",
    # conversations
    "reply_generated": "Respuesta generada",
    "message_sent": "Mensaje enviado",
    "escalation_created": "Escalación creada",
    "escalation_sent": "Consulta enviada al TCP",
    "escalation_resolved": "Escalación resuelta",
    "escalation_retry": "Reintento de escalación",
    "escalation_timeout": "Escalación vencida",
    "tcp_message_received": "Mensaje del TCP recibido",
    "tcp_reply_unmatched": "Respuesta del TCP sin consulta",
    "nlu_invalid_output": "Salida NLU inválida",
    "summary_updated": "Resumen actualizado",
    "audio_transcribed": "Audio transcrito",
    # entities
    "category_created": "Categoría creada",
    "category_updated": "Categoría actualizada",
    "category_deleted": "Categoría eliminada",
    "zone_created": "Zona creada",
    "zone_updated": "Zona actualizada",
    "zone_deleted": "Zona eliminada",
    "supplier_created": "TCP creado",
    "supplier_updated": "TCP actualizado",
    "supplier_deleted": "TCP desactivado",
    "product_created": "Producto creado",
    "product_updated": "Producto actualizado",
    "product_deleted": "Producto eliminado",
    "product_image_added": "Imagen añadida",
    "product_image_main_set": "Imagen principal cambiada",
    "product_image_deleted": "Imagen eliminada",
    "broadcast_group_created": "Grupo creado",
    "broadcast_group_updated": "Grupo actualizado",
    "broadcast_group_deactivated": "Grupo desactivado",
    "broadcast_group_deleted": "Grupo eliminado",
    "broadcast_created": "Campaña creada",
    "broadcast_updated": "Campaña actualizada",
    "broadcast_deleted": "Campaña eliminada",
    "broadcast_strategy_saved": "Estrategia guardada",
    "broadcast_strategy_applied": "Estrategia aplicada",
    # system
    "job_completed": "Trabajo completado",
    "job_failed": "Trabajo fallido",
    "job_rate_limited": "Límite de Telegram",
    "jobs_requeued": "Trabajos reencolados",
    "telegram_flood_cleanup": "Limpieza de flood de Telegram",
    "llm_started": "LLM iniciado",
    "llm_stopped": "LLM detenido",
    "llm_restarted": "LLM reiniciado",
    "llm_model_changed": "Modelo cambiado",
    "llm_ctx_changed": "Contexto cambiado",
}

SEVERITIES = {
    "broadcast_posted": "success",
    "broadcast_cycle_complete": "success",
    "broadcast_publishing_resumed": "success",
    "message_sent": "success",
    "escalation_resolved": "success",
    "job_completed": "success",
    "broadcast_post_failed": "error",
    "job_failed": "error",
    "broadcast_publishing_paused": "warning",
    "broadcast_disabled": "warning",
    "broadcast_no_targets": "warning",
    "escalation_retry": "warning",
    "escalation_timeout": "warning",
    "tcp_reply_unmatched": "warning",
    "nlu_invalid_output": "warning",
    "job_rate_limited": "warning",
    "telegram_flood_cleanup": "warning",
    "escalation_created": "warning",
    "llm_stopped": "warning",
}

ENTITY_LABELS = {
    "conversations": "Conversación",
    "messages": "Mensaje",
    "escalations": "Escalación",
    "suppliers": "TCP",
    "products": "Producto",
    "product_images": "Imagen",
    "broadcast_groups": "Grupo",
    "broadcasts": "Campaña",
    "categories": "Categoría",
    "zones": "Zona",
    "jobs": "Trabajo",
    "settings": "Ajustes",
    "llm": "LLM",
}


def _truncate(value, limit: int = 120) -> str:
    text = str(value).replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _label(event_type: str) -> str:
    if event_type in LABELS:
        return LABELS[event_type]
    return event_type.replace("_", " ").capitalize()


def _severity(event_type: str) -> str:
    if event_type in SEVERITIES:
        return SEVERITIES[event_type]
    if event_type.endswith(("_deleted", "_cancelled")):
        return "warning"
    if event_type.endswith("_created"):
        return "success"
    return "info"


def _entity_label(entity: str | None, entity_id: int | None) -> str | None:
    if not entity:
        return None
    label = ENTITY_LABELS.get(entity, entity.replace("_", " ").capitalize())
    return f"{label} #{entity_id}" if entity_id is not None else label


def _quote(value) -> str:
    return f'"{_truncate(value, 60)}"' if value else ""


def _fields(details: dict) -> str:
    fields = details.get("fields") or []
    return f"campos: {', '.join(str(f) for f in fields)}" if fields else ""


class Presenter:
    """Turn a raw event row into a compact, human-readable Spanish record."""

    def __init__(self, conn):
        self.conn = conn
        self._cache: dict[tuple[str, int], str | None] = {}

    def _name(self, table: str, ident, column: str = "name") -> str | None:
        if ident is None:
            return None
        try:
            key = (table, int(ident))
        except (TypeError, ValueError):
            return None
        if key not in self._cache:
            row = self.conn.execute(
                f"SELECT {column} FROM {table} WHERE id = ?", (key[1],)
            ).fetchone()
            self._cache[key] = row[0] if row else None
        return self._cache[key]

    def _group(self, details: dict) -> str | None:
        return details.get("group_name") or self._name(
            "broadcast_groups", details.get("group_id")
        )

    def _supplier(self, details: dict) -> str | None:
        return details.get("supplier_name") or self._name(
            "suppliers", details.get("supplier_id")
        )

    def _product(self, details: dict) -> str | None:
        return details.get("product_name") or self._name(
            "products", details.get("product_id")
        )

    def _conversation(self, entity_id) -> str:
        return f"Conversación #{entity_id}" if entity_id is not None else "Conversación"

    def summary(
        self, event_type: str, entity, entity_id, details: dict
    ) -> str:
        d = details or {}
        eid = entity_id

        # ------------------------------------------------------- publications
        if event_type == "broadcast_scheduled":
            headline = f"Campaña #{eid}"
            group = self._group(d)
            if group:
                headline += f" → {_quote(group)}"
            parts = [headline]
            if d.get("pass") and d.get("passes"):
                parts.append(f"pase {d['pass']}/{d['passes']}")
            if d.get("next_send_at"):
                parts.append(f"para {d['next_send_at']} UTC")
            return " · ".join(parts)
        if event_type in ("broadcast_posted", "broadcast_post_failed"):
            headline = f"Campaña #{eid}"
            group = self._group(d)
            if group:
                headline += f" → {_quote(group)}"
            parts = [headline]
            if d.get("post_id"):
                parts.append(f"post #{d['post_id']}")
            if d.get("error"):
                parts.append(f"error: {_truncate(d['error'])}")
            return " · ".join(parts)
        if event_type == "broadcast_no_targets":
            return f"Campaña #{eid} · sin grupos elegibles"
        if event_type == "broadcast_cycle_complete":
            passes = d.get("passes")
            tail = f"{passes} pases" if passes else "ciclo cerrado"
            return f"Campaña #{eid} · {tail}"
        if event_type == "broadcast_send_now":
            return f"Campaña #{eid} · envío inmediato solicitado"
        if event_type in ("broadcast_publishing_paused", "broadcast_publishing_resumed"):
            paused = event_type.endswith("_paused")
            headline = "Publicaciones pausadas" if paused else "Publicaciones reanudadas"
            dropped = d.get("queued_dropped") or 0
            return f"{headline} · {dropped} envío(s) retirados" if dropped else headline
        if event_type in ("broadcast_enabled", "broadcast_disabled"):
            state = (
                "habilitada para publicar"
                if event_type.endswith("_enabled")
                else "deshabilitada"
            )
            return f"Campaña #{eid} · {state}"
        if event_type == "broadcasts_reevaluated":
            return (
                f"{d.get('checked', 0)} campañas revisadas · "
                f"{d.get('queued', 0)} encoladas · "
                f"{d.get('waiting', 0)} en espera"
            )

        # ------------------------------------------------------ conversations
        if event_type == "reply_generated":
            parts = [self._conversation(eid)]
            if d.get("intent"):
                confidence = d.get("confidence")
                pct = f" ({round(float(confidence) * 100)} %)" if confidence is not None else ""
                parts.append(f"intención {_quote(d['intent'])}{pct}")
            product = self._product(d)
            if product:
                parts.append(f"producto {_quote(product)}")
            if d.get("escalate"):
                parts.append("escalada a TCP")
            elif d.get("degraded"):
                parts.append("modo degradado")
            return " · ".join(parts)
        if event_type == "message_sent":
            channel = d.get("channel") or "—"
            identity = d.get("identity")
            parts = [f"Mensaje #{eid}", f"canal {channel}"]
            if identity:
                parts.append(f"identidad {identity}")
            if d.get("delay_s") is not None:
                parts.append(f"demora {d['delay_s']} s")
            return " · ".join(parts)
        if event_type == "escalation_created":
            supplier, product = self._supplier(d), self._product(d)
            parts = [f"Escalación #{eid}"]
            if supplier:
                parts.append(f"TCP {_quote(supplier)}")
            if product:
                parts.append(f"producto {_quote(product)}")
            return " · ".join(parts)
        if event_type == "escalation_sent":
            supplier = self._supplier(d)
            identity = d.get("identity")
            parts = [f"Escalación #{eid}"]
            if supplier:
                parts.append(f"TCP {_quote(supplier)}")
            if identity:
                parts.append(f"vía {identity}")
            return " · ".join(parts)
        if event_type == "escalation_resolved":
            supplier = self._supplier(d)
            return " · ".join(
                filter(None, [f"Escalación #{eid}", f"TCP {_quote(supplier)}" if supplier else "", "respuesta recibida"])
            )
        if event_type == "escalation_retry":
            return f"Escalación #{eid} · intento {d.get('attempts', '—')}"
        if event_type == "escalation_timeout":
            return f"Escalación #{eid} · sin respuesta tras {d.get('attempts', 0)} intento(s)"
        if event_type == "tcp_message_received":
            supplier = self._supplier(d)
            parts = ["Mensaje del TCP"]
            if supplier:
                parts.append(_quote(supplier))
            if d.get("message_id"):
                parts.append(f"mensaje #{d['message_id']}")
            return " · ".join(parts)
        if event_type == "tcp_reply_unmatched":
            supplier = self._supplier(d)
            parts = ["Respuesta sin consulta pendiente"]
            if supplier:
                parts.append(_quote(supplier))
            return " · ".join(parts)
        if event_type == "nlu_invalid_output":
            return f"{self._conversation(eid)} · mensaje #{d.get('message_id')} · se usó intención \"otro\""
        if event_type == "summary_updated":
            return f"{self._conversation(eid)} · resumen de {d.get('messages', '—')} mensajes"
        if event_type == "audio_transcribed":
            parts = [f"Mensaje #{eid}"]
            if d.get("duration_s") is not None:
                parts.append(f"audio {d['duration_s']} s")
            if d.get("elapsed_s") is not None:
                parts.append(f"transcrito en {d['elapsed_s']} s")
            if d.get("language"):
                parts.append(f"idioma {d['language']}")
            return " · ".join(parts)

        # ----------------------------------------------------------- entities
        name = d.get("name")
        if event_type == "category_created":
            return f"Categoría {_quote(name)}"
        if event_type in ("category_updated", "zone_updated", "supplier_updated", "product_updated"):
            label = {
                "category_updated": "Categoría",
                "zone_updated": "Zona",
                "supplier_updated": "TCP",
                "product_updated": "Producto",
            }[event_type]
            parts = [f"{label} {_quote(name) if name else f'#{eid}'}"]
            if _fields(d):
                parts.append(_fields(d))
            return " · ".join(parts)
        if event_type == "category_deleted":
            unlinked = d.get("unlinked") or {}
            return (
                f"Categoría {_quote(name)} · {unlinked.get('products', 0)} productos y "
                f"{unlinked.get('groups', 0)} grupos desvinculados"
            )
        if event_type == "zone_created":
            return f"Zona {_quote(name)}"
        if event_type == "zone_deleted":
            return f"Zona {_quote(name)} eliminada"
        if event_type == "supplier_created":
            zones = d.get("zones") or []
            return f"TCP {_quote(name)} · {len(zones)} zona(s)"
        if event_type == "supplier_deleted":
            return f"TCP {_quote(name)} desactivado"
        if event_type == "product_created":
            supplier = self._name("suppliers", d.get("supplier_id"))
            return " · ".join(
                filter(None, [f"Producto {_quote(name)}", f"TCP {_quote(supplier)}" if supplier else ""])
            )
        if event_type == "product_deleted":
            return f"Producto {_quote(name)} · marcado como eliminado"
        if event_type == "product_image_added":
            main = " · principal" if d.get("is_main") else ""
            return f"Producto #{d.get('product_id')} · imagen #{eid}{main}"
        if event_type == "product_image_main_set":
            return f"Producto #{d.get('product_id')} · imagen #{eid} como principal"
        if event_type == "product_image_deleted":
            return f"Producto #{d.get('product_id')} · imagen #{eid} eliminada"
        if event_type == "broadcast_group_created":
            sender = d.get("sender")
            parts = [f"Grupo {_quote(name)}"]
            if sender:
                parts.append(f"remitente {sender}")
            return " · ".join(parts)
        if event_type in (
            "broadcast_group_updated",
            "broadcast_group_deactivated",
            "broadcast_group_deleted",
        ):
            parts = [f"Grupo {_quote(name) if name else f'#{eid}'}"]
            if event_type.endswith("_updated") and _fields(d):
                parts.append(_fields(d))
            if event_type.endswith("_deactivated"):
                parts.append("desactivado")
            if event_type.endswith("_deleted"):
                parts.append("eliminado")
            return " · ".join(parts)
        if event_type == "broadcast_created":
            parts = [f"Campaña #{eid}"]
            if d.get("groups") is not None:
                parts.append(f"{d['groups']} grupo(s)")
            if d.get("channel"):
                parts.append(f"canal {d['channel']}")
            return " · ".join(parts)
        if event_type == "broadcast_updated":
            parts = [f"Campaña #{eid}"]
            if _fields(d):
                parts.append(_fields(d))
            return " · ".join(parts)
        if event_type == "broadcast_deleted":
            return (
                f"Campaña #{eid} eliminada · {d.get('groups', 0)} grupo(s) · "
                f"{d.get('posts', 0)} envío(s)"
            )
        if event_type in ("broadcast_strategy_saved", "broadcast_strategy_applied"):
            values = d.get("values") or d
            parts = []
            if d.get("groups") is not None:
                parts.append(f"{d['groups']} grupos")
            if values.get("max_posts_per_day") is not None:
                parts.append(f"{values['max_posts_per_day']} posts/día")
            if values.get("min_interval_s") is not None and values.get("max_interval_s") is not None:
                parts.append(f"intervalo {values['min_interval_s']}-{values['max_interval_s']} s")
            return " · ".join(parts) or "Estrategia de publicación actualizada"

        # ------------------------------------------------------------- system
        if event_type == "job_completed":
            kind = d.get("job_type")
            return f"Trabajo #{eid}" + (f" ({kind})" if kind else "") + " completado"
        if event_type == "job_failed":
            kind = d.get("job_type")
            parts = [f"Trabajo #{eid}" + (f" ({kind})" if kind else ""), "falló"]
            if d.get("error"):
                parts.append(_truncate(d["error"]))
            return " · ".join(parts)
        if event_type == "job_rate_limited":
            return (
                f"Trabajo #{eid} ({d.get('job_type', '—')}) · "
                f"reintento en {d.get('wait_s', '—')} s "
                f"({d.get('hit', '—')}/{d.get('max_hits', '—')})"
            )
        if event_type == "jobs_requeued":
            return f"{d.get('count', 0)} trabajo(s) reencolado(s) tras un reinicio"
        if event_type == "telegram_flood_cleanup":
            return d.get("summary") or "Posts colgados y cooldown corregidos"
        if event_type == "llm_started":
            return f"Modelo {_quote(d.get('model'))} iniciado"
        if event_type == "llm_stopped":
            return "Servidor LLM detenido"
        if event_type == "llm_restarted":
            return f"Servidor LLM reiniciado · modelo {_quote(d.get('model'))}"
        if event_type == "llm_model_changed":
            return f"Modelo cambiado a {_quote(d.get('model'))}"
        if event_type == "llm_ctx_changed":
            return f"Contexto ajustado a {d.get('ctx', '—')} tokens"

        # Fallback: never leave the row without a summary.
        return self._fallback(d)

    @staticmethod
    def _fallback(details: dict) -> str:
        if not details:
            return "Sin detalles adicionales"
        parts = []
        for key, value in list(details.items())[:3]:
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            parts.append(f"{key}={_truncate(value, 40)}")
        return " · ".join(parts)


def _label_sql() -> tuple[str, list]:
    """SQL expression that mirrors the Spanish label shown in the dashboard.

    Searching only raw columns would make the table feel broken: the operator
    reads "Publicación fallida" but `broadcast_post_failed` does not contain the
    word. Both are searched so either vocabulary works.
    """
    whens = " ".join("WHEN ? THEN ?" for _ in LABELS)
    sql = f"(CASE type {whens} ELSE replace(type, '_', ' ') END)"
    params: list = []
    for event_type, label in LABELS.items():
        params.extend([event_type, label])
    return sql, params


def _filters(window: str, search: str | None) -> tuple[list[str], list]:
    clauses: list[str] = []
    params: list = []
    hours = WINDOWS[window]
    if hours is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        clauses.append("created_at >= ?")
        params.append(cutoff.strftime("%Y-%m-%d %H:%M:%S"))
    term = (search or "").strip().lower()
    if term:
        like = f"%{term}%"
        label_sql, label_params = _label_sql()
        clauses.append(
            "(lower(type) LIKE ? OR lower(source) LIKE ? OR lower(entity) LIKE ? "
            "OR lower(COALESCE(details_json, '')) LIKE ? "
            f"OR lower({label_sql}) LIKE ?)"
        )
        params.extend([like, like, like, like, *label_params, like])
    return clauses, params


def _parse_details(raw) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {"value": value}


@router.get("/logs")
def list_logs(
    category: str | None = None,
    search: str | None = None,
    window: Literal["1h", "24h", "7d", "all"] = "24h",
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    if category is not None and category not in events.CATEGORIES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"category must be one of {events.CATEGORIES}",
        )

    clauses, params = _filters(window, search)
    where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""

    conn = db.connect()
    try:
        counts = {name: 0 for name in events.CATEGORIES}
        for row in conn.execute(
            "SELECT category, COUNT(*) AS n FROM events"
            + where_sql
            + " GROUP BY category",
            params,
        ):
            if row["category"] in counts:
                counts[row["category"]] = row["n"]

        total_sql = "SELECT COUNT(*) FROM events" + where_sql
        total_params = list(params)
        if category:
            total_sql += (" AND " if clauses else " WHERE ") + "category = ?"
            total_params.append(category)
        total = conn.execute(total_sql, total_params).fetchone()[0]

        items_sql = "SELECT * FROM events" + where_sql
        item_params = list(params)
        if category:
            items_sql += (" AND " if clauses else " WHERE ") + "category = ?"
            item_params.append(category)
        items_sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        item_params.extend([limit, offset])

        presenter = Presenter(conn)
        items = []
        for row in conn.execute(items_sql, item_params):
            details = _parse_details(row["details_json"])
            items.append(
                {
                    "id": row["id"],
                    "created_at": row["created_at"],
                    "category": row["category"],
                    "source": row["source"],
                    "type": row["type"],
                    "label": _label(row["type"]),
                    "summary": presenter.summary(
                        row["type"], row["entity"], row["entity_id"], details
                    ),
                    "severity": _severity(row["type"]),
                    "entity": row["entity"],
                    "entity_id": row["entity_id"],
                    "entity_label": _entity_label(row["entity"], row["entity_id"]),
                    "details": details,
                }
            )
    finally:
        conn.close()

    return {
        "items": items,
        "total": total,
        "counts": counts,
        "category_labels": CATEGORY_LABELS,
        "window": window,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
