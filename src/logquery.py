"""`python -m src.logquery` — read the logs back.

The logs are JSONL on purpose, so `grep` and `jq` work. This is the shortcut
for the questions that get asked in practice, and it is the first thing to
reach for when someone says "the agent did something weird ten minutes ago":

    python -m src.logquery sessions                  # who connected, when, how many errors
    python -m src.logquery trace --session sess-9a1c # replay one session end to end
    python -m src.logquery trace --request 7 --session sess-9a1c
    python -m src.logquery trace --order RO-1001     # one order across both streams
    python -m src.logquery errors --since 1h         # what is currently broken
    python -m src.logquery tail -n 30 --follow       # watch it live
    python -m src.logquery audit --store ST-047      # the buyer's view, in the terminal
    python -m src.logquery audit --csv > audit.csv

Every subcommand takes `--json` to print the raw records instead of the
formatted view — that is the escape hatch into jq when this tool does not
have the exact filter someone needs.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
import time
from typing import Any, Iterable

from . import audit
from .eventlog import audit_log, diagnostic_log

_RELATIVE = re.compile(r"^(\d+)([smhd])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _parse_since(value: str | None) -> str | None:
    """`--since` accepts `1h`, `30m`, `2d` or an ISO prefix like `2026-07-31`."""
    if not value:
        return None
    match = _RELATIVE.match(value)
    if match:
        seconds = int(match.group(1)) * _UNIT_SECONDS[match.group(2)]
        moment = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds)
        return moment.isoformat(timespec="milliseconds")
    return value


def _load(streams: str = "both") -> list[dict]:
    records: list[dict] = []
    if streams in ("both", "diagnostic"):
        records.extend(diagnostic_log.read())
    if streams in ("both", "audit"):
        records.extend(audit_log.read())
    records.sort(key=lambda r: (r.get("ts", ""), r.get("seq", 0)))
    return records


def _matches(record: dict, args: argparse.Namespace, order_requests: set[tuple[str, str]] | None) -> bool:
    since = _parse_since(getattr(args, "since", None))
    if since and record.get("ts", "") < since:
        return False
    if getattr(args, "session", None) and record.get("session_id") != args.session:
        if not (record.get("trace") or {}).get("session_id") == args.session:
            return False
    if getattr(args, "request", None) and str(record.get("request_id")) != str(args.request):
        if not str((record.get("trace") or {}).get("request_id")) == str(args.request):
            return False
    if getattr(args, "tool", None) and record.get("tool") != args.tool:
        return False
    if getattr(args, "errors", False) and record.get("outcome") != "error" and "error" not in record:
        return False
    if getattr(args, "grep", None) and args.grep.lower() not in json.dumps(record, default=repr).lower():
        return False
    if order_requests is not None and not _belongs_to_order(record, args.order, order_requests):
        return False
    return True


def _belongs_to_order(record: dict, order_id: str, order_requests: set[tuple[str, str]]) -> bool:
    """An order's trail spans both streams: the audit entries name the order
    directly, and the tool call that raised it is found through the
    session/request ids the audit entry carries."""
    if record.get("order_id") == order_id:
        return True
    if order_id in json.dumps(record, default=repr):
        return True
    key = (str(record.get("session_id")), str(record.get("request_id")))
    return key in order_requests


def _order_requests(order_id: str) -> set[tuple[str, str]]:
    keys = set()
    for entry in audit.entries(order_id=order_id, newest_first=False):
        trace = entry.get("trace") or {}
        if trace.get("session_id"):
            keys.add((str(trace["session_id"]), str(trace.get("request_id"))))
    return keys


# --- formatting -------------------------------------------------------------


def _short_ts(ts: str) -> str:
    return ts[11:23] if len(ts) > 23 else ts


def _args_repr(arguments: dict | None) -> str:
    if not arguments:
        return ""
    return ", ".join(f"{k}={v!r}" for k, v in arguments.items())


def _outcome(record: dict) -> str:
    outcome = record.get("outcome")
    if outcome == "error":
        err = record.get("error") or {}
        return f"ERROR {err.get('type', '')}: {err.get('message', '')}"
    if outcome:
        return outcome
    return ""


def _format_diagnostic(record: dict) -> list[str]:
    event = record.get("event", "")
    where = f"{record.get('session_id', '-')}/req-{record.get('request_id', '-')}"
    head = f"{_short_ts(record.get('ts', ''))}  {where}  "
    duration = f"  {record['duration_ms']}ms" if record.get("duration_ms") is not None else ""

    if event in ("tool_call", "tool_call_started"):
        marker = "->" if event == "tool_call_started" else "<-"
        line = (f"{head}{marker} {record.get('tool')}({_args_repr(record.get('arguments'))})"
                f"  {_outcome(record)}{duration}")
        lines = [line]
        for call in record.get("upstream_calls") or []:
            lines.append(f"{' ' * 16}   . storelink.{call.get('method')}({_call_args(call)})"
                         f" {call.get('outcome')} {call.get('duration_ms')}ms")
        result = record.get("result") or {}
        if "value" in result or "preview" in result:
            body = json.dumps(result.get("value")) if "value" in result else result.get("preview")
            lines.append(f"{' ' * 16}   = {_clip(body)}")
        return lines
    if event == "mcp_request":
        client = record.get("client") or {}
        who = f"  client={client.get('name')} {client.get('version')}" if client.get("name") else ""
        return [f"{head}   {record.get('method')}  {_outcome(record)}{duration}{who}"]
    if event == "server_started":
        return [f"{_short_ts(record.get('ts', ''))}  ---  server started"
                f" (transport={record.get('transport')}, pid={record.get('pid')}, run={record.get('run_id')})"]
    extra = {k: v for k, v in record.items()
             if k not in ("ts", "stream", "event", "seq", "run_id", "session_id", "request_id")}
    return [f"{head}   {event}  {_clip(json.dumps(extra, default=repr))}"]


def _call_args(call: dict) -> str:
    positional = (repr(a) for a in call.get("args") or [])
    keyword = (f"{k}={v!r}" for k, v in (call.get("kwargs") or {}).items())
    return ", ".join([*positional, *keyword])


def _clip(text: str | None, width: int = 160) -> str:
    if not text:
        return ""
    text = text.replace("\n", " ")
    return text if len(text) <= width else text[: width - 1] + "…"


def _format_audit(record: dict) -> list[str]:
    actor = record.get("actor") or {}
    trace = record.get("trace") or {}
    where = f"  [{trace.get('session_id')}/req-{trace.get('request_id')}]" if trace.get("session_id") else ""
    return [f"{_short_ts(record.get('ts', ''))}  AUDIT  {record.get('event')}  "
            f"{record.get('order_id', '')}  by {actor.get('name', '?')}  "
            f"{record.get('summary', '')}{where}"]


def _print(records: Iterable[dict], as_json: bool) -> None:
    for record in records:
        if as_json:
            print(json.dumps(record, ensure_ascii=False, default=repr))
            continue
        formatter = _format_audit if record.get("stream") == "audit" else _format_diagnostic
        for line in formatter(record):
            print(line.rstrip())


# --- subcommands ------------------------------------------------------------


def cmd_sessions(args: argparse.Namespace) -> None:
    sessions: dict[str, dict[str, Any]] = {}
    since = _parse_since(args.since)
    for record in diagnostic_log.read():
        if since and record.get("ts", "") < since:
            continue
        sid = record.get("session_id")
        if not sid:
            continue
        row = sessions.setdefault(sid, {"first": record["ts"], "last": record["ts"], "calls": 0,
                                        "errors": 0, "client": None, "tools": {}})
        row["last"] = record["ts"]
        if record.get("client"):
            row["client"] = record["client"]
        if record.get("event") == "tool_call":
            row["calls"] += 1
            row["tools"][record.get("tool")] = row["tools"].get(record.get("tool"), 0) + 1
            if record.get("outcome") == "error":
                row["errors"] += 1
    if args.json:
        print(json.dumps(sessions, indent=2, default=repr))
        return
    if not sessions:
        print("No sessions logged yet.")
        return
    print(f"{'session':<22} {'first seen':<24} {'calls':>5} {'err':>4}  client / tools")
    for sid, row in sorted(sessions.items(), key=lambda kv: kv[1]["first"]):
        client = row["client"] or {}
        tools = ", ".join(f"{name}×{n}" for name, n in sorted(row["tools"].items(), key=lambda kv: -kv[1]))
        print(f"{sid:<22} {row['first'][:23]:<24} {row['calls']:>5} {row['errors']:>4}  "
              f"{client.get('name', '?')} {client.get('version', '')} | {tools}")


def cmd_trace(args: argparse.Namespace) -> None:
    order_requests = _order_requests(args.order) if args.order else None
    records = [r for r in _load(args.stream) if _matches(r, args, order_requests)]
    if args.limit:
        records = records[-args.limit:]
    if not records:
        print("No matching entries.", file=sys.stderr)
    _print(records, args.json)


def cmd_errors(args: argparse.Namespace) -> None:
    args.errors = True
    args.order = None
    cmd_trace(args)


def cmd_tail(args: argparse.Namespace) -> None:
    args.order = None
    seen = 0
    records = [r for r in _load(args.stream) if _matches(r, args, None)]
    _print(records[-args.n:], args.json)
    seen = len(records)
    while args.follow:
        time.sleep(1.0)
        records = [r for r in _load(args.stream) if _matches(r, args, None)]
        if len(records) > seen:
            _print(records[seen:], args.json)
            seen = len(records)


def cmd_audit(args: argparse.Namespace) -> None:
    records = audit.entries(order_id=args.order, store_id=args.store, sku=args.sku,
                            since=_parse_since(args.since), newest_first=not args.csv)
    if args.csv:
        writer = csv.writer(sys.stdout)
        writer.writerow(audit.CSV_COLUMNS)
        for record in records:
            writer.writerow(audit.as_csv_row(record))
        return
    if args.json:
        _print(records, True)
        return
    if not records:
        print("No audit entries match.")
        return
    for record in records:
        actor = record.get("actor") or {}
        verified = "" if actor.get("verified") else " (unverified)"
        print(f"{record.get('ts')}  {record.get('summary')}")
        print(f"{'':<26}by {actor.get('name')}{verified}"
              + (f"  |  evidence: {json.dumps(record['evidence'])}" if record.get("evidence") else ""))


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="print raw JSONL records")
    parser.add_argument("--since", help="1h / 30m / 2d, or an ISO timestamp prefix")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.logquery", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    subs = parser.add_subparsers(dest="command", required=True)

    sessions = subs.add_parser("sessions", help="one line per client session seen")
    _add_common(sessions)
    sessions.set_defaults(func=cmd_sessions)

    trace = subs.add_parser("trace", help="replay a session, a request, or an order")
    _add_common(trace)
    trace.add_argument("--session", help="session id (see `sessions`)")
    trace.add_argument("--request", help="JSON-RPC request id within a session")
    trace.add_argument("--order", help="order id, e.g. RO-1001 — spans both streams")
    trace.add_argument("--tool", help="only calls to this tool")
    trace.add_argument("--grep", help="substring match over the whole record")
    trace.add_argument("--errors", action="store_true", help="only failures")
    trace.add_argument("--stream", choices=["both", "diagnostic", "audit"], default="both")
    trace.add_argument("--limit", type=int, help="keep only the last N entries")
    trace.set_defaults(func=cmd_trace)

    errors = subs.add_parser("errors", help="every failure, newest last")
    _add_common(errors)
    errors.add_argument("--session")
    errors.add_argument("--tool")
    errors.add_argument("--grep")
    errors.add_argument("--request")
    errors.add_argument("--stream", choices=["both", "diagnostic", "audit"], default="both")
    errors.add_argument("--limit", type=int)
    errors.set_defaults(func=cmd_errors)

    tail = subs.add_parser("tail", help="the most recent entries, optionally following")
    _add_common(tail)
    tail.add_argument("-n", type=int, default=20)
    tail.add_argument("--follow", "-f", action="store_true")
    tail.add_argument("--session")
    tail.add_argument("--tool")
    tail.add_argument("--grep")
    tail.add_argument("--request")
    tail.add_argument("--errors", action="store_true")
    tail.add_argument("--stream", choices=["both", "diagnostic", "audit"], default="both")
    tail.set_defaults(func=cmd_tail)

    audit_cmd = subs.add_parser("audit", help="the buyer's audit trail, in plain language")
    _add_common(audit_cmd)
    audit_cmd.add_argument("--order")
    audit_cmd.add_argument("--store")
    audit_cmd.add_argument("--sku")
    audit_cmd.add_argument("--csv", action="store_true", help="CSV export, oldest first")
    audit_cmd.set_defaults(func=cmd_audit)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
