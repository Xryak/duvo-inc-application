"""Minimal browser page where a human approves or rejects pending orders.

Served on a separate port by a background thread of the same process, so it
works regardless of which MCP transport is in use (over stdio we cannot
prompt on the console — stdin/stdout carry the MCP protocol).

Deliberately dependency-free (stdlib http.server) and unauthenticated:
inside Korral's network the pilot plan is to bind it to an internal host
reachable only by the buying team. Add SSO/reverse-proxy auth before wider
rollout.
"""

from __future__ import annotations

import html
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from .orders import OrderLedger

_PAGE = """<!doctype html><meta charset="utf-8">
<title>StoreLink MCP — Order Approvals</title>
<style>
 body {{ font: 15px/1.5 system-ui, sans-serif; max-width: 780px; margin: 2rem auto; padding: 0 1rem; }}
 table {{ border-collapse: collapse; width: 100%; }}
 td, th {{ border-bottom: 1px solid #ddd; padding: .5rem .6rem; text-align: left; vertical-align: top; }}
 button {{ padding: .3rem .9rem; cursor: pointer; }}
 .ok {{ background: #1a7f37; color: #fff; border: 0; border-radius: 4px; }}
 .no {{ background: #cf222e; color: #fff; border: 0; border-radius: 4px; }}
 .empty {{ color: #666; }}
</style>
<h1>Pending replenishment orders</h1>
<p>Orders proposed by the Duvo agent. Nothing reaches StoreLink until you approve it.</p>
{body}
<p><a href="/">Refresh</a></p>
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


def _render(ledger: OrderLedger) -> str:
    pending = sorted(ledger.pending(), key=lambda o: o.created_at)
    if not pending:
        return _PAGE.format(body='<p class="empty">No orders waiting for approval.</p>')
    rows = "".join(
        _ROW.format(**{k: html.escape(str(v)) for k, v in o.to_dict().items()})
        for o in pending
    )
    header = ("<table><tr><th>Order</th><th>Store</th><th>SKU</th>"
              "<th>Qty</th><th>Agent's reason</th><th></th></tr>")
    return _PAGE.format(body=header + rows + "</table>")


def _make_handler(ledger: OrderLedger):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep stdout/stderr quiet
            pass

        def _html(self, content: str, status: int = 200):
            body = content.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._html(_render(ledger))

        def do_POST(self):
            if self.path != "/decision":
                self._html("Not found", 404)
                return
            length = int(self.headers.get("Content-Length", 0))
            form = parse_qs(self.rfile.read(length).decode())
            order_id = form.get("order_id", [""])[0]
            action = form.get("action", [""])[0]
            try:
                if action == "approve":
                    ledger.approve(order_id)
                elif action == "reject":
                    ledger.reject(order_id)
            except (KeyError, ValueError):
                pass  # already decided or unknown; the refreshed list shows truth
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()

    return Handler


def start_approvals_server(ledger: OrderLedger, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), _make_handler(ledger))
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="approvals-ui")
    thread.start()
    return server
