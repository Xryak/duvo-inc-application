"""Keep every test's log writes inside a tmp dir, and isolated from each other."""

import pytest

from src import audit, server
from src.eventlog import audit_log, diagnostic_log


@pytest.fixture(autouse=True)
def isolated_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("STORELINK_LOG_DIR", str(tmp_path / "logs"))
    # EventLog caches its resolved path on first write; clear it so this
    # test's records land in this test's directory.
    monkeypatch.setattr(diagnostic_log, "_path", None, raising=False)
    monkeypatch.setattr(audit_log, "_path", None, raising=False)
    audit._positions_seen.clear()
    # The tool functions share one module-level ledger. Start each test with an
    # empty one so orders raised by one test never show up as another test's
    # open_orders.
    server.ledger._orders.clear()
    yield
