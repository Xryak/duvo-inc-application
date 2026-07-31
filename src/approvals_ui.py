"""Minimal browser pages for the human in the loop: approvals, and the audit trail.

Served on a separate port by a background thread of the same process, so it
works regardless of which MCP transport is in use (over stdio we cannot
prompt on the console — stdin/stdout carry the MCP protocol).

Two pages, for the same person:

- `/` — pending orders to approve or reject. The approver types their name
  once; it is remembered in a cookie and recorded against every decision.
- `/audit` — the buyer's audit trail: every proposal, decision and StoreLink
  submission, newest first, filterable by store / SKU / order, exportable as
  CSV at `/audit.csv` with the same filters. This is the read side of
  `src/audit.py`; the same records are printed by
  `python -m src.logquery audit`.

Deliberately dependency-free (stdlib http.server) and unauthenticated:
inside Korral's network the pilot plan is to bind it to an internal host
reachable only by the buying team. Add SSO/reverse-proxy auth before wider
rollout — until then a decision's `actor.name` is self-asserted, and both the
page and the stored record say so.
"""

from __future__ import annotations

import csv
import html
import io
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import audit, diagnostics
from .orders import OrderLedger

_AUDIT_PAGE_LIMIT = 300

_SHELL = """<!doctype html><meta charset="utf-8">
<title>StoreLink MCP — {title}</title>
<style>
 body {{ font: 15px/1.5 system-ui, sans-serif; max-width: 1040px; margin: 2rem auto; padding: 0 1rem; }}
 table {{ border-collapse: collapse; width: 100%; }}
 td, th {{ border-bottom: 1px solid #ddd; padding: .5rem .6rem; text-align: left; vertical-align: top; }}
 button {{ padding: .3rem .9rem; cursor: pointer; }}
 input[type=text] {{ padding: .3rem .4rem; font: inherit; }}
 .ok {{ background: #1a7f37; color: #fff; border: 0; border-radius: 4px; }}
 .no {{ background: #cf222e; color: #fff; border: 0; border-radius: 4px; }}
 .empty {{ color: #666; }}
 nav {{ margin-bottom: 1.2rem; }}
 nav a {{ margin-right: 1rem; }}
 small {{ color: #555; }}
 .who {{ white-space: nowrap; }}
 .agent {{ color: #6639ba; }}
 .human {{ color: #0969da; }}
 .bad {{ color: #cf222e; font-weight: 600; }}
 .evidence {{ color: #555; }}
 form.filters input {{ margin-right: .4rem; }}
</style>
<nav><a href="/">Pending approvals</a><a href="/audit">Audit trail</a></nav>
{body}
"""

_ROW = """<tr>
 <td><b>{order_id}</b><br><small>{created_at}</small></td>
 <td>{store_id}</td>
 <td>{sku}<br><small>{sku_name}</small></td>
 <td>{quantity}</td>
 <td>{reason}</td>
 <td>
  <form method="post" action="/decision" style="display:inline">
   <input type="hidden" name="order_id" value="{order_id}">
   <button class="ok" name="action" value="approve">Approve</button>
   <button class="no" name="action" value="reject">Reject</button>
  </form>
 </td>
</tr>"""

_NAME_BOX = """<form method="post" action="/whoami">
 <label>Approving as <input type="text" name="approver" value="{approver}" placeholder="your name"></label>
 <button>Save</button><br>
 <small>Recorded against every decision you make. The page is unauthenticated in the pilot,
 so this name is self-asserted — the audit trail stores it marked as unverified.</small>
</form>"""

_EVENT_LABELS = {
    audit.PROPOSED: "Agent proposed",
    audit.APPROVED: "Approved",
    audit.REJECTED: "Rejected",
    audit.SUBMITTED: "Sent to StoreLink",
    audit.SUBMISSION_FAILED: "StoreLink refused",
}

_AUDIT_FILTERS = (
    ("order_id", "order id"),
    ("store_id", "store id"),
    ("sku", "SKU"),
    ("since", "since (YYYY-MM-DD)"),
)


def _esc(value) -> str:
    return html.escape(str(value)) if value is not None else ""


def _render_pending(ledger: OrderLedger, approver: str) -> str:
    pending = sorted(ledger.pending(), key=lambda o: o.created_at)
    body = [
        "<h1>Pending replenishment orders</h1>",
        "<p>Orders proposed by the Duvo agent. Nothing reaches StoreLink until you approve it.</p>",
        _NAME_BOX.format(approver=_esc(approver)),
    ]
    if not pending:
        body.append('<p class="empty">No orders waiting for approval.</p>')
    else:
        body.append("<table><tr><th>Order</th><th>Store</th><th>SKU</th>"
                    "<th>Qty</th><th>Agent's reason</th><th></th></tr>")
        body.append("".join(_ROW.format(**{k: _esc(v) for k, v in o.to_dict().items()}) for o in pending))
        body.append("</table>")
    body.append('<p><a href="/">Refresh</a></p>')
    return _SHELL.format(title="Order Approvals", body="".join(body))


def _evidence_html(record: dict) -> str:
    """The numbers the agent had read when it proposed the order — a buyer
    checking a stated reason should not have to take the agent's prose on trust."""
    ev = record.get("evidence")
    if not ev:
        return '<br><small class="evidence">No stock position was read for this SKU before the order.</small>'
    cover = ev.get("days_of_cover")
    return (
        '<br><small class="evidence">Agent had read: {on_hand} on hand, selling {vel}/day, '
        "{cover} days of cover, {lead}-day lead time{risk}.</small>"
    ).format(
        on_hand=_esc(ev.get("on_hand")),
        vel=_esc(ev.get("avg_daily_units_7d")),
        cover="no" if cover is None else _esc(cover),
        lead=_esc(ev.get("supplier_lead_time_days")),
        risk=" — flagged as stockout risk" if ev.get("stockout_risk") else "",
    )


def _audit_row(record: dict) -> str:
    actor = record.get("actor") or {}
    event = record.get("event", "")
    detail = _esc(record.get("summary", ""))
    if event == audit.PROPOSED:
        detail = f"Reason: {_esc(record.get('reason'))}{_evidence_html(record)}"
    elif event == audit.SUBMITTED:
        detail = (f"StoreLink order <b>{_esc(record.get('storelink_order_id'))}</b>, "
                  f"expected {_esc(record.get('expected_delivery'))}")
    elif event == audit.SUBMISSION_FAILED:
        detail = f'<span class="bad">{detail}</span>'
    return (
        "<tr>"
        f"<td><small>{_esc(record.get('ts'))}</small></td>"
        f'<td class="{_esc(actor.get("kind"))}">{_esc(_EVENT_LABELS.get(event, event))}</td>'
        f"<td>{_esc(record.get('order_id'))}</td>"
        f"<td>{_esc(record.get('store_id'))}</td>"
        f"<td>{_esc(record.get('sku'))}<br><small>{_esc(record.get('sku_name'))}</small></td>"
        f"<td>{_esc(record.get('quantity'))}</td>"
        f'<td class="who">{_esc(actor.get("name"))}'
        f'{"" if actor.get("verified") else "<br><small>unverified</small>"}</td>'
        f"<td>{detail}</td>"
        "</tr>"
    )


def _filters_from(query: dict[str, list[str]]) -> dict[str, str | None]:
    return {key: (query.get(key, [""])[0].strip() or None) for key, _ in _AUDIT_FILTERS}


def _render_audit(query: dict[str, list[str]]) -> str:
    filters = _filters_from(query)
    records = audit.entries(limit=_AUDIT_PAGE_LIMIT, **filters)
    qs = "&".join(f"{k}={html.escape(v)}" for k, v in filters.items() if v)
    body = [
        "<h1>Audit trail</h1>",
        "<p>Every replenishment order the agent proposed, what you decided, and what reached "
        "StoreLink. Append-only — entries are never edited or removed.</p>",
        '<form class="filters" method="get" action="/audit">',
        *[f'<input type="text" name="{key}" value="{_esc(filters[key] or "")}" placeholder="{placeholder}">'
          for key, placeholder in _AUDIT_FILTERS],
        "<button>Filter</button> ",
        f'<a href="/audit.csv{"?" + qs if qs else ""}">Download CSV</a>',
        "</form>",
    ]
    if not records:
        body.append('<p class="empty">No audit entries match.</p>')
    else:
        body.append("<table><tr><th>When (UTC)</th><th>What</th><th>Order</th><th>Store</th>"
                    "<th>SKU</th><th>Qty</th><th>Who</th><th>Details</th></tr>")
        body.append("".join(_audit_row(r) for r in records))
        body.append("</table>")
        if len(records) == _AUDIT_PAGE_LIMIT:
            body.append(f'<p class="empty">Showing the {_AUDIT_PAGE_LIMIT} most recent entries — '
                        "narrow with a filter, or download the CSV for the full history.</p>")
    return _SHELL.format(title="Audit Trail", body="".join(body))


def _audit_csv(query: dict[str, list[str]]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(audit.CSV_COLUMNS)
    for record in audit.entries(newest_first=False, **_filters_from(query)):
        writer.writerow(audit.as_csv_row(record))
    return out.getvalue().encode("utf-8")


def _not_found() -> str:
    return _SHELL.format(title="Not found", body="<p>Not found.</p>")


def _make_handler(ledger: OrderLedger):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep stdout/stderr quiet
            pass

        # --- plumbing ---

        def _approver(self) -> str:
            morsel = SimpleCookie(self.headers.get("Cookie", "")).get("approver")
            return morsel.value if morsel else ""

        def _send(self, body: bytes, content_type: str, status: int = 200, headers: tuple = ()):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _html(self, content: str, status: int = 200):
            self._send(content.encode(), "text/html; charset=utf-8", status)

        def _redirect(self, location: str, headers: tuple = ()):
            self.send_response(303)
            self.send_header("Location", location)
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()

        def _form(self) -> dict[str, list[str]]:
            length = int(self.headers.get("Content-Length", 0))
            return parse_qs(self.rfile.read(length).decode())

        # --- routes ---

        def do_GET(self):
            url = urlparse(self.path)
            query = parse_qs(url.query)
            if url.path == "/":
                self._html(_render_pending(ledger, self._approver()))
            elif url.path == "/audit":
                self._html(_render_audit(query))
            elif url.path == "/audit.csv":
                self._send(_audit_csv(query), "text/csv; charset=utf-8",
                           headers=(("Content-Disposition", 'attachment; filename="storelink-audit.csv"'),))
            else:
                self._html(_not_found(), 404)

        def do_POST(self):
            if self.path == "/whoami":
                name = self._form().get("approver", [""])[0][:80]
                self._redirect("/", headers=(("Set-Cookie", f"approver={name}; Path=/; Max-Age=2592000"),))
                return
            if self.path != "/decision":
                self._html(_not_found(), 404)
                return
            form = self._form()
            order_id = form.get("order_id", [""])[0]
            action = form.get("action", [""])[0]
            actor = audit.human_actor(self._approver(), source=self.client_address[0])
            try:
                if action == "approve":
                    ledger.approve(order_id, actor)
                elif action == "reject":
                    ledger.reject(order_id, actor)
            except (KeyError, ValueError) as exc:
                # Already decided or unknown; the refreshed list shows truth.
                # Still worth a diagnostic line: "I clicked and nothing
                # happened" is nearly always a double-submit or a stale tab.
                diagnostics.note("approval_ignored", order_id=order_id, action=action,
                                 reason=f"{type(exc).__name__}: {exc}")
            except Exception as exc:  # StoreLink refused an order the human approved
                diagnostics.note("approval_failed", order_id=order_id, action=action, error=str(exc))
                self._html(_SHELL.format(
                    title="Order not placed",
                    body=(f"<h1>Order {_esc(order_id)} was not placed</h1>"
                          f'<p class="bad">StoreLink refused it: {_esc(exc)}</p>'
                          '<p>Your approval and the failure are both in the '
                          '<a href="/audit">audit trail</a>. No stock has been ordered.</p>'),
                ), 502)
                return
            self._redirect("/")

    return Handler


def start_approvals_server(ledger: OrderLedger, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), _make_handler(ledger))
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="approvals-ui")
    thread.start()
    return server
