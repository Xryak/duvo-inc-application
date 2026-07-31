"""Tests for the two log streams: the FDE's diagnostic trail and the buyer's audit trail."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from src import audit, logquery, server
from src.approvals_ui import _audit_csv, _render_audit
from src.diagnostics import ToolCallLogger
from src.eventlog import EventLog, audit_log, diagnostic_log
from src.orders import OrderLedger
from src.storelink import NotFound, StubStoreLinkClient

ST, SKU = "ST-014", "SKU-0451"


class FakeConnection:
    """One client connection. Mirrors the SDK: no transport session id over
    stdio, plus a `state` dict that outlives each request."""

    session_id = None

    def __init__(self):
        self.state = {}
        self.client_params = SimpleNamespace(clientInfo=SimpleNamespace(name="claude-code", version="2.1"))


class FakeSession:
    """The SDK builds one of these per inbound message, not per connection."""

    def __init__(self, connection):
        self._connection = connection

    @property
    def client_params(self):
        return self._connection.client_params


def call_tool(tool: str, arguments: dict, *, session=None, request_id=1):
    """Drive a tool the way the wire does — through the logging middleware.

    `session` here is really the connection: a fresh `FakeSession` is built per
    call, as the SDK does, so session-id correlation is exercised honestly.
    """
    ctx = SimpleNamespace(
        method="tools/call",
        params={"name": tool, "arguments": arguments},
        request_id=request_id,
        session=FakeSession(session or FakeConnection()),
        protocol_version="2026-07-28",
    )

    async def call_next(c):
        return getattr(server, c.params["name"])(**c.params["arguments"])

    return asyncio.run(ToolCallLogger()(ctx, call_next))


def diagnostics_of(event: str) -> list[dict]:
    return [r for r in diagnostic_log.read() if r["event"] == event]


# --- FDE stream: what was called, how, in what context ---------------------


def test_tool_call_records_arguments_outcome_and_correlation_ids():
    session = FakeConnection()
    call_tool("get_stock_position", {"store_id": ST, "sku": SKU}, session=session, request_id=7)

    started = diagnostics_of("tool_call_started")[-1]
    done = diagnostics_of("tool_call")[-1]

    assert started["tool"] == done["tool"] == "get_stock_position"
    assert done["arguments"] == {"store_id": ST, "sku": SKU}
    assert done["outcome"] == "ok"
    assert done["duration_ms"] >= 0
    assert done["request_id"] == "7"
    assert done["session_id"] == started["session_id"]
    assert done["client"] == {"name": "claude-code", "version": "2.1"}


def test_calls_on_one_connection_share_a_session_id():
    """The SDK hands a fresh session object to every request, so the id has to
    come off the connection — otherwise every call looks like its own session."""
    connection = FakeConnection()
    call_tool("list_stores", {}, session=connection, request_id=1)
    call_tool("list_stores", {}, session=connection, request_id=2)
    first, second = diagnostics_of("tool_call")[-2:]
    assert first["session_id"] == second["session_id"]
    assert (first["request_id"], second["request_id"]) == ("1", "2")


def test_two_connections_get_distinct_session_ids():
    call_tool("list_stores", {}, session=FakeConnection())
    call_tool("list_stores", {}, session=FakeConnection())
    first, second = diagnostics_of("tool_call")[-2:]
    assert first["session_id"] != second["session_id"]


def test_transport_session_id_is_reused_when_there_is_one():
    """Streamable HTTP names its own sessions; sharing that id lines our log
    up with the HTTP access log."""
    connection = FakeConnection()
    connection.session_id = "mcp-session-abc123"
    call_tool("list_stores", {}, session=connection)
    assert diagnostics_of("tool_call")[-1]["session_id"] == "mcp-session-abc123"


def test_tool_call_captures_the_result_the_agent_saw():
    call_tool("get_stock_position", {"store_id": ST, "sku": SKU})
    result = diagnostics_of("tool_call")[-1]["result"]
    body = result.get("value") or json.loads(result["preview"] + "}")
    assert body["on_hand"] == 12


def test_upstream_storelink_calls_are_attached_to_the_tool_call():
    """The 'in what context' part: which upstream calls produced this answer."""
    call_tool("get_stock_position", {"store_id": ST, "sku": SKU})
    upstream = diagnostics_of("tool_call")[-1]["upstream_calls"]
    methods = [c["method"] for c in upstream]
    assert {"get_inventory", "get_sku", "get_supplier", "get_pos_transactions"} <= set(methods)
    assert all(c["outcome"] == "ok" and c["duration_ms"] >= 0 for c in upstream)


def test_failed_tool_call_is_logged_and_still_raises():
    with pytest.raises(NotFound):
        call_tool("get_stock_position", {"store_id": "ST-999", "sku": SKU})
    done = diagnostics_of("tool_call")[-1]
    assert done["outcome"] == "error"
    assert done["error"]["type"] == "NotFound"
    assert "ST-999" in done["error"]["message"]
    # the upstream call that failed is recorded on its own line too
    assert any(r["method"] == "get_inventory" for r in diagnostics_of("upstream_call"))


def test_tool_error_returned_as_a_result_is_still_counted_as_an_error():
    """On the real wire the SDK catches a tool's exception and answers with
    `isError: true`; the middleware must not read that as a success."""
    ctx = SimpleNamespace(method="tools/call", params={"name": "get_stock_position", "arguments": {}},
                          request_id=9, session=FakeSession(FakeConnection()),
                          protocol_version="2026-07-28")

    async def call_next(c):
        return {"content": [{"type": "text", "text": "Error executing tool: Unknown store 'ST-999'."}],
                "isError": True}

    asyncio.run(ToolCallLogger()(ctx, call_next))
    done = diagnostics_of("tool_call")[-1]
    assert done["outcome"] == "error"
    assert "ST-999" in done["error"]["message"]


def test_credential_shaped_arguments_are_redacted():
    with pytest.raises(TypeError):
        call_tool("list_stores", {"store_key": "super-secret"})
    assert diagnostics_of("tool_call")[-1]["arguments"] == {"store_key": "[redacted]"}


def test_non_tool_traffic_is_logged_thinly():
    ctx = SimpleNamespace(method="tools/list", params=None, request_id=2,
                          session=FakeConnection(), protocol_version="2026-07-28")

    async def call_next(c):
        return {"tools": []}

    asyncio.run(ToolCallLogger()(ctx, call_next))
    assert diagnostics_of("mcp_request")[-1]["method"] == "tools/list"


# --- buyer stream: the audit trail -----------------------------------------


def test_proposal_records_reason_evidence_and_a_trace_back_to_the_request():
    session = FakeConnection()
    call_tool("get_stock_position", {"store_id": ST, "sku": SKU}, session=session, request_id=3)
    result = call_tool("create_replenishment_order",
                       {"store_id": ST, "sku": SKU, "quantity": 120,
                        "reason": "12 on hand, 9.5/day, 2-day lead"},
                       session=session, request_id=4)

    entry = audit.entries(order_id=result["order_id"])[0]
    assert entry["event"] == audit.PROPOSED
    assert entry["actor"] == {"kind": "agent", "name": "Duvo agent via claude-code 2.1", "verified": False}
    assert entry["reason"] == "12 on hand, 9.5/day, 2-day lead"
    assert entry["evidence"]["on_hand"] == 12          # what the agent had actually read
    assert entry["evidence"]["stockout_risk"] is True
    assert entry["trace"]["request_id"] == "4"          # joins to the diagnostic stream
    assert "120" in entry["summary"]


def test_approval_records_who_and_what_reached_storelink():
    ledger = OrderLedger(StubStoreLinkClient())
    order = ledger.create(ST, SKU, "Fjord Smoked Salmon 200g", 120, "12 on hand, 9/day")
    ledger.approve(order.order_id, audit.human_actor("Jana K.", source="10.0.0.7"))

    events = [e["event"] for e in audit.entries(order_id=order.order_id, newest_first=False)]
    assert events == [audit.PROPOSED, audit.APPROVED, audit.SUBMITTED]

    approved, submitted = audit.entries(order_id=order.order_id, newest_first=False)[1:]
    assert approved["actor"]["name"] == "Jana K."
    assert approved["actor"]["kind"] == "human"
    assert approved["actor"]["verified"] is False       # page is unauthenticated in the pilot
    assert approved["actor"]["source"] == "10.0.0.7"
    assert submitted["storelink_order_id"].startswith("SL-")
    assert submitted["expected_delivery"]
    assert ledger.get(order.order_id).decided_by == "Jana K."


def test_rejection_is_recorded_and_says_nothing_was_sent():
    ledger = OrderLedger(StubStoreLinkClient())
    order = ledger.create(ST, SKU, "Fjord Smoked Salmon 200g", 60, "hunch")
    ledger.reject(order.order_id, audit.human_actor("Petr H."))

    entry = audit.entries(order_id=order.order_id)[0]
    assert entry["event"] == audit.REJECTED
    assert "Nothing was sent to StoreLink" in entry["summary"]
    assert not audit.entries(order_id=order.order_id, event=audit.SUBMITTED)


def test_storelink_refusing_an_approved_order_is_visible_to_the_buyer():
    client = StubStoreLinkClient()
    ledger = OrderLedger(client)
    order = ledger.create(ST, SKU, "Fjord Smoked Salmon 200g", 60, "restock")

    def boom(*a, **kw):
        raise NotFound("supplier account closed")

    client.create_replenishment = boom
    with pytest.raises(NotFound):
        ledger.approve(order.order_id, audit.human_actor("Jana K."))

    trail = audit.entries(order_id=order.order_id, newest_first=False)
    assert [e["event"] for e in trail] == [audit.PROPOSED, audit.APPROVED, audit.SUBMISSION_FAILED]
    assert "was NOT placed" in trail[-1]["summary"]


def test_audit_entries_filter_by_store_sku_and_order():
    ledger = OrderLedger(StubStoreLinkClient())
    a = ledger.create("ST-047", "8847291", "Madeta Butter 250g", 480, "gap")
    ledger.create("ST-102", "8847291", "Madeta Butter 250g", 40, "gap")

    assert {e["order_id"] for e in audit.entries(store_id="ST-047")} == {a.order_id}
    assert len(audit.entries(sku="8847291")) == 2
    assert len(audit.entries(order_id=a.order_id)) == 1
    assert audit.entries(since="2999-01-01") == []


# --- reading the logs back --------------------------------------------------


def test_logquery_trace_by_order_spans_both_streams(capsys):
    session = FakeConnection()
    call_tool("get_stock_position", {"store_id": ST, "sku": SKU}, session=session, request_id=11)
    result = call_tool("create_replenishment_order",
                       {"store_id": ST, "sku": SKU, "quantity": 90, "reason": "cover < lead time"},
                       session=session, request_id=12)

    logquery.main(["trace", "--order", result["order_id"]])
    out = capsys.readouterr().out
    assert "create_replenishment_order" in out       # the agent's call
    assert "AUDIT  order_proposed" in out            # the buyer's entry
    assert result["order_id"] in out


def test_logquery_session_replay_and_error_filter(capsys):
    session = FakeConnection()
    call_tool("get_sales_history", {"store_id": ST, "sku": SKU, "days": 3}, session=session, request_id=1)
    with pytest.raises(ValueError):
        call_tool("get_sales_history", {"store_id": ST, "sku": SKU, "days": 90},
                  session=session, request_id=2)
    session_id = diagnostics_of("tool_call")[-1]["session_id"]

    logquery.main(["trace", "--session", session_id])
    replay = capsys.readouterr().out
    assert "get_sales_history" in replay and "ERROR ValueError" in replay

    logquery.main(["errors", "--session", session_id, "--json"])
    errors = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert errors and all(e.get("outcome") == "error" for e in errors)


def test_logquery_sessions_summary(capsys):
    call_tool("list_stores", {})
    logquery.main(["sessions"])
    out = capsys.readouterr().out
    assert "claude-code" in out and "list_stores×1" in out


def test_logquery_audit_csv_is_flat_and_ordered(capsys):
    ledger = OrderLedger(StubStoreLinkClient())
    order = ledger.create(ST, SKU, "Fjord Smoked Salmon 200g", 30, "restock")
    ledger.approve(order.order_id, audit.human_actor("Jana K."))

    logquery.main(["audit", "--csv"])
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0].startswith("ts,event,order_id")
    assert lines[1].split(",")[1] == audit.PROPOSED      # oldest first for the export
    assert "Jana K." in lines[2]


def test_since_accepts_relative_windows():
    assert logquery._parse_since("2026-07-01") == "2026-07-01"
    assert logquery._parse_since("1h") > "2026-01-01"
    assert logquery._parse_since(None) is None


# --- the audit page ---------------------------------------------------------


def test_audit_page_shows_evidence_and_who_decided():
    session = FakeConnection()
    call_tool("get_stock_position", {"store_id": "ST-047", "sku": "8847291"}, session=session)
    result = call_tool("create_replenishment_order",
                       {"store_id": "ST-047", "sku": "8847291", "quantity": 480,
                        "reason": "2 on hand, 20/day, 2-day lead"},
                       session=session)
    server.ledger.approve(result["order_id"], audit.human_actor("Jana K.", source="10.0.0.7"))

    page = _render_audit({})
    assert "Agent had read: 2 on hand" in page
    assert "flagged as stockout risk" in page
    assert "Jana K." in page and "unverified" in page
    assert "Sent to StoreLink" in page

    csv_bytes = _audit_csv({"store_id": ["ST-047"]}).decode()
    assert csv_bytes.splitlines()[0].startswith("ts,event,order_id")
    assert "ST-047" in csv_bytes and "ST-014" not in csv_bytes


def test_audit_page_escapes_agent_supplied_text():
    ledger = OrderLedger(StubStoreLinkClient())
    ledger.create(ST, SKU, "Fjord Smoked Salmon 200g", 10, "<script>alert(1)</script>")
    page = _render_audit({})
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


# --- the sink itself --------------------------------------------------------


def test_diagnostic_log_rotates_and_audit_log_does_not(tmp_path, monkeypatch):
    monkeypatch.setenv("STORELINK_LOG_DIR", str(tmp_path))
    rotating = EventLog("rot", rotate_at=400)
    for i in range(40):
        rotating.emit("noise", i=i, padding="x" * 50)
    assert rotating.path.with_suffix(".jsonl.1").exists()   # rolled, one generation kept
    assert rotating.path.stat().st_size <= 400
    read_back = rotating.read()
    assert read_back[-1]["i"] == 39                          # newest survives
    assert len(read_back) < 40                               # older records aged out, by design
    assert audit_log.rotate_at == 0                          # the audit trail never ages out


def test_torn_lines_do_not_break_reading(tmp_path, monkeypatch):
    monkeypatch.setenv("STORELINK_LOG_DIR", str(tmp_path))
    log = EventLog("torn")
    log.emit("first")
    with log.path.open("a") as fh:
        fh.write('{"ts": "2026-07-31T00:00:0\n')   # killed mid-write
    log.emit("second")
    assert [r["event"] for r in log.read()] == ["first", "second"]
