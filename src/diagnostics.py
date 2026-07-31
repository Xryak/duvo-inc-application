"""The Forward Deployed Engineer's stream: what the agent called, how, and in what context.

Everything here writes to `logs/diagnostic.jsonl`. The unit of debugging is a
**request** — one `tools/call` from the agent — and requests group into a
**session**, one client connection. Every record carries `session_id` and
`request_id` so `python -m src.logquery` can replay either.

Three layers are instrumented, and the layering is the point:

1. `ToolCallLogger` (MCP middleware) — the wire truth. Fires for every inbound
   message, before params are even validated, so a call that fails validation
   or hits an unknown tool still shows up. Emits `tool_call_started` up front
   (so a hung call is visible while it hangs) and `tool_call` on completion
   with duration, outcome and the result the agent actually received.
2. `instrument_storelink` — wraps the StoreLink client so every upstream call
   made *while serving a tool* is attached to that tool call. This is the
   "in what context" part: when `get_stock_position` returns a surprising
   number, the FDE can see the four upstream calls it was derived from.
3. `note` — free-form breadcrumbs from anywhere in the server (the approvals
   page uses it), correlated when there is a request in scope.

Nothing here can fail a tool call: the middleware re-raises whatever the
handler raised, and the sink swallows its own IO errors.
"""

from __future__ import annotations

import functools
import json
import os
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable
from weakref import WeakKeyDictionary

from .eventlog import diagnostic_log, redact

#: Cap on how much of a tool's return value is captured, in bytes of JSON.
#: The result is what the agent saw, so it is usually the single most useful
#: field when explaining a decision — but a 60-day sales history is large.
#: Set STORELINK_DIAG_RESULT_BYTES=0 to stop capturing results entirely.
_MAX_RESULT_BYTES = int(os.getenv("STORELINK_DIAG_RESULT_BYTES", "4096"))

#: Set STORELINK_DIAG=off to silence this stream. The audit stream is not
#: switchable — it is the record of record.
_ENABLED = os.getenv("STORELINK_DIAG", "on").lower() not in ("off", "0", "false", "no")


@dataclass
class RequestScope:
    """The in-flight tool call, for whatever code runs beneath it."""

    session_id: str
    request_id: str
    tool: str
    arguments: dict[str, Any]
    client: dict | None = None
    started_at: float = field(default_factory=time.perf_counter)
    upstream: list[dict] = field(default_factory=list)

    def elapsed_ms(self) -> float:
        return round((time.perf_counter() - self.started_at) * 1000, 1)


_scope: ContextVar[RequestScope | None] = ContextVar("storelink_request_scope", default=None)

#: Fallback for session ids minted for connections the transport did not name
#: (stdio has no session id of its own). Keyed weakly on the connection so the
#: mapping dies with it.
_minted_session_ids: "WeakKeyDictionary[Any, str]" = WeakKeyDictionary()


def current_scope() -> RequestScope | None:
    """The tool call being served on this task/thread, if any."""
    return _scope.get()


def current_trace() -> dict[str, str]:
    """Correlation ids for the current tool call — stamped onto audit entries
    so a buyer's audit row can be traced back to the exact agent request."""
    scope = _scope.get()
    if scope is None:
        return {}
    return {"session_id": scope.session_id, "request_id": scope.request_id}


def current_client() -> dict | None:
    """Which client is driving the current tool call — used to name the actor
    on an audit entry ("Duvo agent via claude-code 2.1")."""
    scope = _scope.get()
    return scope.client if scope else None


def _emit(event: str, **fields: Any) -> None:
    if _ENABLED:
        diagnostic_log.emit(event, **fields)


def note(event: str, **fields: Any) -> None:
    """Record a diagnostic breadcrumb, correlated to the current request if there is one."""
    _emit(event, **current_trace(), **fields)


# --- 1. MCP middleware ------------------------------------------------------


_SESSION_STATE_KEY = "storelink.session_id"


def _session_id(session: Any) -> str:
    """A stable id for one client connection.

    The SDK builds a fresh `ServerSession` per inbound message, so the id has
    to hang off the `Connection` behind it — that is the object that lives for
    the whole conversation, and its `state` dict exists for exactly this. Order
    of preference:

    1. the transport's own session id (streamable HTTP sets one; reusing it
       means our log lines up with any HTTP access log);
    2. a uuid minted once into `connection.state` — the stdio case;
    3. a weak map keyed on the connection, if a future SDK drops `state`.

    Getting this wrong is not cosmetic: every request would look like its own
    session and `logquery trace --session` would return one call.
    """
    if session is None:
        return "sess-direct"
    connection = getattr(session, "_connection", None) or session
    transport_id = getattr(connection, "session_id", None)
    if transport_id:
        return str(transport_id)
    state = getattr(connection, "state", None)
    if isinstance(state, dict):
        if _SESSION_STATE_KEY not in state:
            state[_SESSION_STATE_KEY] = f"sess-{uuid.uuid4().hex[:12]}"
        return state[_SESSION_STATE_KEY]
    try:
        existing = _minted_session_ids.get(connection)
        if existing is None:
            existing = f"sess-{uuid.uuid4().hex[:12]}"
            _minted_session_ids[connection] = existing
        return existing
    except TypeError:  # not weak-referenceable
        return f"sess-{id(connection):x}"


def _client_info(session: Any) -> dict | None:
    params = getattr(session, "client_params", None)
    info = getattr(params, "clientInfo", None) or getattr(params, "client_info", None)
    if info is None:
        return None
    return {"name": getattr(info, "name", None), "version": getattr(info, "version", None)}


def _tool_error(result: Any) -> dict | None:
    """A tool that raises does not reach the middleware as an exception.

    The SDK catches it and answers the client with `isError: true` and the
    message as content — a successful *protocol* exchange carrying a failure.
    Without this, `logquery errors` would show nothing for the most common
    failure there is, so the flag is read back off the result.
    """
    if isinstance(result, dict):
        flagged = result.get("isError") or result.get("is_error")
        content = result.get("content")
    else:
        flagged = getattr(result, "isError", None) or getattr(result, "is_error", None)
        content = getattr(result, "content", None)
    if not flagged:
        return None
    message = ""
    if isinstance(content, list) and content:
        first = content[0]
        message = (first.get("text") if isinstance(first, dict) else getattr(first, "text", "")) or ""
    return {"type": "tool_error", "message": message or "tool reported an error"}


def _result_digest(result: Any) -> dict:
    """A bounded capture of what the tool returned."""
    if _MAX_RESULT_BYTES == 0:
        return {"captured": False}
    try:
        body = json.dumps(redact(result), ensure_ascii=False, default=repr)
    except (TypeError, ValueError):
        body = repr(result)
    digest: dict[str, Any] = {"bytes": len(body)}
    if len(body) > _MAX_RESULT_BYTES:
        digest["truncated"] = True
        digest["preview"] = body[:_MAX_RESULT_BYTES]
        if isinstance(result, dict):
            digest["keys"] = list(result)
        elif isinstance(result, list):
            digest["items"] = len(result)
    else:
        digest["value"] = redact(result)
    return digest


class ToolCallLogger:
    """MCP middleware that records every inbound message.

    Registered as `mcp.middleware.append(ToolCallLogger())`. Non-tool traffic
    (`initialize`, `tools/list`, notifications) is recorded thinly — enough to
    answer "did this client connect, and what did it think we could do", which
    is the first question in most support threads — while `tools/call` gets the
    full treatment.
    """

    async def __call__(self, ctx: Any, call_next: Callable) -> Any:
        session_id = _session_id(getattr(ctx, "session", None))
        method = getattr(ctx, "method", "?")
        request_id = str(getattr(ctx, "request_id", None) or "-")

        if method != "tools/call":
            return await self._log_plain(ctx, call_next, session_id, method, request_id)

        params = getattr(ctx, "params", None) or {}
        tool = params.get("name", "?")
        arguments = redact(params.get("arguments") or {})
        client = _client_info(getattr(ctx, "session", None))
        scope = RequestScope(
            session_id=session_id, request_id=request_id, tool=tool, arguments=arguments, client=client
        )

        common = {
            "session_id": session_id,
            "request_id": request_id,
            "tool": tool,
            "client": client,
            "protocol_version": getattr(ctx, "protocol_version", None),
        }
        _emit("tool_call_started", arguments=arguments, **common)

        token = _scope.set(scope)
        try:
            result = await call_next(ctx)
        except BaseException as exc:  # noqa: BLE001 — observed, then re-raised untouched
            _emit(
                "tool_call",
                outcome="error",
                arguments=arguments,
                duration_ms=scope.elapsed_ms(),
                error={"type": type(exc).__name__, "message": str(exc)},
                upstream_calls=scope.upstream or None,
                **common,
            )
            raise
        else:
            error = _tool_error(result)
            _emit(
                "tool_call",
                outcome="error" if error else "ok",
                arguments=arguments,
                duration_ms=scope.elapsed_ms(),
                error=error,
                result=_result_digest(result),
                upstream_calls=scope.upstream or None,
                **common,
            )
            return result
        finally:
            _scope.reset(token)

    async def _log_plain(self, ctx, call_next, session_id: str, method: str, request_id: str):
        started = time.perf_counter()
        try:
            result = await call_next(ctx)
        except BaseException as exc:  # noqa: BLE001
            _emit(
                "mcp_request",
                session_id=session_id,
                request_id=request_id,
                method=method,
                outcome="error",
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
                error={"type": type(exc).__name__, "message": str(exc)},
            )
            raise
        fields: dict[str, Any] = {}
        if method == "initialize":
            # The one record that says who is on the other end of this session.
            fields["client"] = _client_info(getattr(ctx, "session", None))
            fields["params"] = redact(getattr(ctx, "params", None))
        _emit(
            "mcp_request",
            session_id=session_id,
            request_id=request_id,
            method=method,
            outcome="ok",
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
            **fields,
        )
        return result


# --- 2. upstream (StoreLink) calls -----------------------------------------

_UPSTREAM_METHODS = (
    "list_stores",
    "get_sku",
    "get_supplier",
    "get_inventory",
    "get_pos_transactions",
    "create_replenishment",
    "get_replenishment",
)


def instrument_storelink(client: Any) -> Any:
    """Wrap a StoreLink client so its calls are logged and attached to the
    tool call that caused them.

    A wrapper rather than edits to `storelink.py`: that module stands in for
    Korral's API, and it should stay free of our observability. Swap the stub
    for a real HTTPS client and this instrumentation still applies.
    """
    for name in _UPSTREAM_METHODS:
        original = getattr(client, name, None)
        if original is None or getattr(original, "_storelink_instrumented", False):
            continue
        setattr(client, name, _wrap_upstream(name, original))
    return client


def _wrap_upstream(name: str, original: Callable) -> Callable:
    @functools.wraps(original)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        scope = _scope.get()
        call: dict[str, Any] = {"method": name, "args": redact(list(args)), "kwargs": redact(kwargs)}
        try:
            result = original(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001
            call["outcome"] = "error"
            call["error"] = {"type": type(exc).__name__, "message": str(exc)}
            call["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
            _record_upstream(scope, call)
            raise
        call["outcome"] = "ok"
        call["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
        if isinstance(result, list):
            call["rows"] = len(result)
        _record_upstream(scope, call)
        return result

    wrapper._storelink_instrumented = True  # type: ignore[attr-defined]
    return wrapper


def _record_upstream(scope: RequestScope | None, call: dict) -> None:
    if scope is not None:
        # Bounded: a 60-day history walk is one call, but a future tool could
        # loop. Keep the tool_call record readable.
        if len(scope.upstream) < 50:
            scope.upstream.append(call)
        elif len(scope.upstream) == 50:
            scope.upstream.append({"method": "...", "note": "further upstream calls omitted"})
    # Errors are worth a line of their own even mid-request: they are what an
    # FDE greps for, and they survive a request that never returns.
    if call.get("outcome") == "error" or scope is None:
        _emit("upstream_call", **current_trace(), **call)


def log_server_start(transport: str, **fields: Any) -> None:
    _emit("server_started", transport=transport, pid=os.getpid(), **fields)
