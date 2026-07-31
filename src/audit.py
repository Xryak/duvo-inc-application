"""The buyer's stream: an audit trail of everything that touched a real order.

Different reader, different rules from `diagnostics.py`. A Korral buyer asking
"what did this thing do on my behalf last week, and who signed off?" should not
have to read MCP internals — so `logs/audit.jsonl` holds only events with
business meaning, each one carrying:

- **what** happened, in a `summary` sentence written at the time of writing, so
  the file is readable without this code;
- **who** did it — `actor.kind` is `agent` or `human`, and for a human, the
  name they gave on the approvals page plus where they were;
- **why** — the agent's stated `reason`, and the `evidence`: the stock numbers
  the agent had actually read for that store and SKU when it proposed the
  order. A reason is a claim; the evidence is what the claim was based on;
- **where to look next** — `trace.session_id` / `trace.request_id`, the join
  key into the FDE's diagnostic log.

Never rotated, never sampled, never switchable off (see `eventlog.EventLog`).
The buyer-facing view is the `/audit` page served by `approvals_ui.py`;
`python -m src.logquery audit` prints the same thing.

Pilot honesty: the approvals page is unauthenticated, so `actor.name` on a
human decision is self-asserted, and every record says so via
`actor.verified: false`. Put the page behind SSO and that flips — the record
shape does not change.
"""

from __future__ import annotations

from typing import Any, Iterable

from .diagnostics import current_client, current_trace
from .eventlog import audit_log

PROPOSED = "order_proposed"
APPROVED = "order_approved"
REJECTED = "order_rejected"
SUBMITTED = "order_submitted_to_storelink"
SUBMISSION_FAILED = "order_submission_failed"

#: Stock positions the agent has read this session, keyed (store_id, sku).
#: Bounded so a long-running server cannot grow it without limit.
_positions_seen: dict[tuple[str, str], dict] = {}
_POSITION_CACHE_MAX = 200


def note_position_seen(store_id: str, sku: str, position: dict) -> None:
    """Remember the numbers the agent just read, to attach as evidence if it
    goes on to propose an order for this store+SKU."""
    if len(_positions_seen) >= _POSITION_CACHE_MAX:
        _positions_seen.pop(next(iter(_positions_seen)), None)
    _positions_seen[(store_id, sku)] = {
        "read_at": position.get("on_hand_last_updated"),
        "on_hand": position.get("on_hand"),
        "avg_daily_units_7d": position.get("avg_daily_units_7d"),
        "days_of_cover": position.get("days_of_cover"),
        "supplier_lead_time_days": position.get("supplier_lead_time_days"),
        "stockout_risk": position.get("stockout_risk"),
    }


def _agent_actor() -> dict:
    client = current_client()
    name = "Duvo agent"
    if client and client.get("name"):
        version = f" {client['version']}" if client.get("version") else ""
        name = f"Duvo agent via {client['name']}{version}"
    return {"kind": "agent", "name": name, "verified": False}


def human_actor(name: str, source: str | None = None) -> dict:
    """An actor for a person who clicked a button on the approvals page."""
    return {
        "kind": "human",
        "name": (name or "").strip() or "unnamed (approvals page)",
        "via": "approvals page",
        "source": source,
        "verified": False,  # page is unauthenticated in the pilot
    }


def _order_fields(order: Any) -> dict:
    return {
        "order_id": order.order_id,
        "store_id": order.store_id,
        "sku": order.sku,
        "sku_name": order.sku_name,
        "quantity": order.quantity,
    }


def record_proposed(order: Any) -> dict:
    evidence = _positions_seen.get((order.store_id, order.sku))
    return audit_log.emit(
        PROPOSED,
        **_order_fields(order),
        actor=_agent_actor(),
        reason=order.reason,
        evidence=evidence,
        summary=(
            f"Agent proposed ordering {order.quantity} × {order.sku_name} "
            f"for {order.store_id} — awaiting your approval."
        ),
        trace=current_trace() or None,
    )


def record_decision(order: Any, *, approved: bool, actor: dict) -> dict:
    verb = "Approved" if approved else "Rejected"
    return audit_log.emit(
        APPROVED if approved else REJECTED,
        **_order_fields(order),
        actor=actor,
        reason=order.reason,
        summary=(
            f"{verb} by {actor['name']}: {order.quantity} × {order.sku_name} for {order.store_id}."
            + ("" if approved else " Nothing was sent to StoreLink.")
        ),
        proposed_at=order.created_at,
    )


def record_submitted(order: Any, *, actor: dict) -> dict:
    return audit_log.emit(
        SUBMITTED,
        **_order_fields(order),
        actor=actor,
        storelink_order_id=order.storelink_order_id,
        expected_delivery=order.expected_delivery,
        summary=(
            f"Sent to StoreLink as {order.storelink_order_id} — "
            f"{order.quantity} × {order.sku_name} to {order.store_id}, "
            f"expected {order.expected_delivery}."
        ),
    )


def record_submission_failed(order: Any, *, actor: dict, error: BaseException) -> dict:
    return audit_log.emit(
        SUBMISSION_FAILED,
        **_order_fields(order),
        actor=actor,
        error={"type": type(error).__name__, "message": str(error)},
        summary=(
            f"StoreLink REJECTED the approved order {order.order_id} "
            f"({order.quantity} × {order.sku_name} for {order.store_id}): {error}. "
            "The order was NOT placed — raise it again or call the supplier."
        ),
    )


# --- reading ---------------------------------------------------------------


def entries(
    *,
    order_id: str | None = None,
    store_id: str | None = None,
    sku: str | None = None,
    event: str | None = None,
    since: str | None = None,
    newest_first: bool = True,
    limit: int | None = None,
) -> list[dict]:
    """Filtered audit records. Every filter is an exact match except `since`,
    an ISO timestamp prefix compared lexically (ISO-8601 UTC sorts correctly)."""
    rows: Iterable[dict] = audit_log.read()
    rows = [
        r
        for r in rows
        if (order_id is None or r.get("order_id") == order_id)
        and (store_id is None or r.get("store_id") == store_id)
        and (sku is None or r.get("sku") == sku)
        and (event is None or r.get("event") == event)
        and (since is None or r.get("ts", "") >= since)
    ]
    rows = list(rows)
    if newest_first:
        rows.reverse()
    return rows[:limit] if limit else rows


CSV_COLUMNS = (
    "ts",
    "event",
    "order_id",
    "store_id",
    "sku",
    "sku_name",
    "quantity",
    "actor_kind",
    "actor_name",
    "reason",
    "storelink_order_id",
    "expected_delivery",
    "summary",
)


def as_csv_row(record: dict) -> list[str]:
    """One audit record flattened for the CSV export a buyer hands to finance."""
    actor = record.get("actor") or {}
    flat = {**record, "actor_kind": actor.get("kind", ""), "actor_name": actor.get("name", "")}
    return [str(flat.get(col, "") or "") for col in CSV_COLUMNS]
