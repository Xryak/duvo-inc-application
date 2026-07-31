"""Append-only JSONL event sinks shared by the two log streams.

The server writes two separate streams, on purpose — they have different
readers, different retention needs, and different failure modes:

- `diagnostic.jsonl` — for the Forward Deployed Engineer. Verbose, keyed by
  session and request id, rotated, safe to delete. Answers "what did the agent
  call, with what arguments, in what context, and what came back".
- `audit.jsonl` — for the Korral buyer. Sparse, business-level, one entry per
  thing that happened to real money. Never rotated or truncated.

Both are line-delimited JSON so they can be grepped, tailed, shipped to a log
collector, or read by `src/logquery.py` without a database. Every record
carries `ts`, `stream`, `event`, `seq` and `run_id`; a record's remaining
fields are the caller's.

Writes are line-at-a-time under a lock and flushed immediately, so a tail -f
or the audit page sees events as they happen. Volume here is a handful of
records per agent turn, so the synchronous write is not worth optimising away;
if this ever fronts a busy multi-tenant deployment, put a queue behind
`EventLog.emit` and keep the record shape.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Iterator

#: Identifies one server process. Restarts get a new id, so an FDE can tell
#: "the agent retried" from "the server came back up under it".
RUN_ID = f"run-{uuid.uuid4().hex[:8]}"

DIAGNOSTIC = "diagnostic"
AUDIT = "audit"

_REDACTED = "[redacted]"
_SECRET_HINTS = ("key", "token", "secret", "password", "authorization", "credential")


def log_dir() -> Path:
    """Where both streams are written. Created on demand."""
    path = Path(os.getenv("STORELINK_LOG_DIR", "logs")).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def redact(value: Any, _depth: int = 0) -> Any:
    """Copy `value` with anything that looks like a credential removed.

    Tool arguments are logged verbatim because that is the point of the
    diagnostic stream, so the redaction has to happen here rather than at each
    call site. StoreLink store keys never reach a tool argument today (they are
    resolved server-side by `StoreKeyProvider`) — this is the guard that keeps
    that true if the interface grows.
    """
    if _depth > 6:
        return "[nested]"
    if isinstance(value, dict):
        return {
            k: _REDACTED if any(h in str(k).lower() for h in _SECRET_HINTS) else redact(v, _depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v, _depth + 1) for v in value[:200]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    # Dates, dataclasses, exceptions... anything else becomes text here rather
    # than at the top level, so one odd leaf cannot turn a whole record into a
    # repr string.
    return str(value)


def _jsonable(value: Any) -> Any:
    """Best-effort JSON coercion — a log write must never raise into a tool."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


class EventLog:
    """One append-only JSONL file.

    `rotate_at` is the size in bytes past which the file is rolled to `.1`
    (one generation kept). Pass 0 to never rotate — what the audit stream
    does, since an audit trail that quietly drops its own history is not an
    audit trail.
    """

    def __init__(self, name: str, *, rotate_at: int = 0):
        self.name = name
        self.rotate_at = rotate_at
        self._lock = threading.Lock()
        self._seq = itertools.count(1)
        self._path: Path | None = None

    @property
    def path(self) -> Path:
        # Resolved lazily so tests (and a redeployed container) can point
        # STORELINK_LOG_DIR somewhere else before the first write.
        if self._path is None:
            self._path = log_dir() / f"{self.name}.jsonl"
        return self._path

    def emit(self, event: str, **fields: Any) -> dict:
        """Append one record and return it (handy for tests and for callers
        that want to echo the record's ids back to the agent)."""
        record = {
            "ts": now_iso(),
            "stream": self.name,
            "event": event,
            "seq": next(self._seq),
            "run_id": RUN_ID,
            **{k: _jsonable(v) for k, v in fields.items() if v is not None},
        }
        line = json.dumps(record, ensure_ascii=False, default=repr)
        with self._lock:
            try:
                self._maybe_rotate(len(line) + 1)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                # A broken log must not break a buyer's order. The failure is
                # visible in the file's absence; the tool call proceeds.
                pass
        return record

    def _maybe_rotate(self, incoming: int) -> None:
        if not self.rotate_at:
            return
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size + incoming <= self.rotate_at:
            return
        self.path.replace(self.path.with_suffix(".jsonl.1"))

    def read(self, *, limit: int | None = None) -> list[dict]:
        """All records, oldest first, rotated generation included.

        Malformed lines (a torn write from a killed process) are skipped
        rather than raising — a query tool that dies on one bad line is
        useless exactly when you need it.
        """
        records: list[dict] = []
        for path in (self.path.with_suffix(".jsonl.1"), self.path):
            records.extend(_read_file(path))
        if limit is not None:
            return records[-limit:]
        return records


def _read_file(path: Path) -> Iterator[dict]:
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    yield parsed
    except OSError:
        return


# Rotation cap for the diagnostic stream only (10 MB ≈ a few hundred thousand
# tool calls). The audit stream is never rotated.
_DIAG_ROTATE_AT = int(os.getenv("STORELINK_LOG_MAX_BYTES", str(10 * 1024 * 1024)))

diagnostic_log = EventLog(DIAGNOSTIC, rotate_at=_DIAG_ROTATE_AT)
audit_log = EventLog(AUDIT)
