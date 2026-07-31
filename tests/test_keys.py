"""Per-store key handling: secret loading, weekly rotation, missing credentials.

The two stories Korral IT judges us on:
(a) a key rotates while a request is in flight -> reload secrets, retry once,
    succeed transparently; if the secrets file is stale too, fail as KeyExpired
    with a message that says what happened and that nothing was processed.
(b) the agent asks for a store we hold no credential for -> fail up front as
    MissingStoreKey, before any work (and before any order is created).
"""

import json
import os

import pytest

from src import server
from src.eventlog import diagnostic_log
from src.orders import OrderLedger, PENDING
from src.storelink import (
    FileStoreKeyProvider,
    KeyExpired,
    MissingStoreKey,
    NotFound,
    StaticStoreKeyProvider,
    StoreLinkError,
    StubStoreLinkClient,
)

ST, SKU = "ST-014", "SKU-0451"


def write_keys(path, keys):
    path.write_text(json.dumps(keys))


@pytest.fixture
def keys_file(tmp_path):
    path = tmp_path / "storelink_keys.json"
    write_keys(path, {ST: "key-week-A"})
    return path


# --- secret loading -------------------------------------------------------

def test_file_provider_loads_keys(keys_file):
    provider = FileStoreKeyProvider(str(keys_file))
    assert provider.get_key(ST) == "key-week-A"
    assert provider.has_key(ST) and not provider.has_key("ST-001")


def test_file_provider_missing_store_is_informative(keys_file):
    provider = FileStoreKeyProvider(str(keys_file))
    with pytest.raises(MissingStoreKey) as exc:
        provider.get_key("ST-001")
    assert "ST-001" in str(exc.value)
    assert "Korral IT" in str(exc.value)


def test_file_provider_refuses_to_start_without_secrets(tmp_path):
    with pytest.raises(StoreLinkError) as exc:
        FileStoreKeyProvider(str(tmp_path / "nope.json"))
    assert "nope.json" in str(exc.value)


def test_weekly_rotation_picked_up_without_restart(keys_file):
    provider = FileStoreKeyProvider(str(keys_file))
    assert provider.get_key(ST) == "key-week-A"
    write_keys(keys_file, {ST: "key-week-B"})
    os.utime(keys_file, ns=(1, 1))  # guarantee the stamp changes
    assert provider.get_key(ST) == "key-week-B"


def test_malformed_overwrite_keeps_last_good_keys(keys_file):
    provider = FileStoreKeyProvider(str(keys_file))
    assert provider.get_key(ST) == "key-week-A"
    keys_file.write_text("{ truncated mid-wri")
    os.utime(keys_file, ns=(2, 2))
    assert provider.get_key(ST) == "key-week-A"  # served from last good load


# --- (a) key rotates while a request is in flight -------------------------

def test_rotation_in_flight_is_retried_transparently(keys_file):
    client = StubStoreLinkClient(keys=FileStoreKeyProvider(str(keys_file)))
    client.get_inventory(ST, SKU)  # StoreLink now knows key-week-A

    # Korral IT rotates: StoreLink switches keys and overwrites the mounted
    # file — but with an unchanged stat stamp, so the provider's cache is
    # stale, exactly the in-flight race.
    stamp = os.stat(keys_file).st_mtime_ns
    client.rotate_key(ST, "key-week-B")
    write_keys(keys_file, {ST: "key-week-B"})
    os.utime(keys_file, ns=(stamp, stamp))

    inv = client.get_inventory(ST, SKU)  # stale key -> 401 -> reload -> retry
    assert inv["on_hand"] == 12


def test_stale_secrets_after_rotation_fail_as_key_expired(keys_file):
    client = StubStoreLinkClient(keys=FileStoreKeyProvider(str(keys_file)))
    client.get_inventory(ST, SKU)
    client.rotate_key(ST, "key-week-B")  # rotation never reached our file

    with pytest.raises(KeyExpired) as exc:
        client.get_inventory(ST, SKU)
    message = str(exc.value)
    assert ST in message and "expired" in message and "NOT processed" in message
    assert "key-week" not in message  # never leak key material

    # Recovery: once IT lands the new key in the file, calls work again.
    write_keys(keys_file, {ST: "key-week-B"})
    assert client.get_inventory(ST, SKU)["on_hand"] == 12


def test_rotation_during_human_approval_leaves_order_pending(keys_file):
    """The write path: approval hits StoreLink; a stale key must not lose
    the order — it stays pending and can be approved again after the fix."""
    client = StubStoreLinkClient(keys=FileStoreKeyProvider(str(keys_file)))
    ledger = OrderLedger(client)
    order = ledger.create(ST, SKU, "Fjord Smoked Salmon 200g", 120, "12 on hand, 9/day")

    client.get_inventory(ST, SKU)
    client.rotate_key(ST, "key-week-B")
    with pytest.raises(KeyExpired):
        ledger.approve(order.order_id)
    assert ledger.get(order.order_id).status == PENDING
    assert client._orders == {}  # StoreLink never saw it

    write_keys(keys_file, {ST: "key-week-B"})
    approved = ledger.approve(order.order_id)
    assert approved.status == "submitted"


# --- (b) store without credentials ----------------------------------------

def test_uncredentialed_store_fails_before_any_work():
    client = StubStoreLinkClient(keys=StaticStoreKeyProvider({"ST-001": "k1"}))
    with pytest.raises(MissingStoreKey):
        client.get_inventory(ST, SKU)
    with pytest.raises(MissingStoreKey):
        client.create_replenishment(ST, SKU, 100)
    assert client._orders == {}


def test_unknown_store_stays_not_found_even_without_keys():
    client = StubStoreLinkClient(keys=StaticStoreKeyProvider({}))
    with pytest.raises(NotFound):
        client.get_inventory("ST-999", SKU)


def test_list_stores_reports_credentialed_flag():
    client = StubStoreLinkClient(keys=StaticStoreKeyProvider({"ST-001": "k1"}))
    flags = {s["store_id"]: s["credentialed"] for s in client.list_stores()}
    assert flags["ST-001"] is True
    assert flags[ST] is False


def test_tool_surface_blocks_uncredentialed_store(monkeypatch):
    monkeypatch.setattr(
        server, "client", StubStoreLinkClient(keys=StaticStoreKeyProvider({"ST-001": "k1"}))
    )
    with pytest.raises(MissingStoreKey) as exc:
        server.get_stock_position("ST-030", SKU)
    assert "ST-030" in str(exc.value)

    with pytest.raises(MissingStoreKey):
        server.create_replenishment_order("ST-030", SKU, 100, "restock")
    assert server.ledger.open_for("ST-030", SKU) == []  # no phantom pending order


# --- key events are visible to the FDE ------------------------------------

def _key_events():
    return [r for r in diagnostic_log.read() if r["event"].startswith("storelink_key")]


def test_transparent_rotation_retry_is_visible_in_the_diagnostic_log(keys_file):
    """A retry the agent never notices still has to be answerable later:
    'did this week's rotation cost us anything?' is a logquery question."""
    client = StubStoreLinkClient(keys=FileStoreKeyProvider(str(keys_file)))
    client.get_inventory(ST, SKU)

    stamp = os.stat(keys_file).st_mtime_ns
    client.rotate_key(ST, "key-week-B")
    write_keys(keys_file, {ST: "key-week-B"})
    os.utime(keys_file, ns=(stamp, stamp))
    client.get_inventory(ST, SKU)

    retries = [r for r in _key_events() if r["event"] == "storelink_key_rotation_retry"]
    assert [r["store_id"] for r in retries] == [ST]
    reloads = [r for r in diagnostic_log.read() if r["event"] == "storelink_keys_loaded"]
    assert reloads[-1]["changed"] == [ST]      # which credential moved, not its value


def test_key_expiry_is_logged_before_it_is_raised(keys_file):
    client = StubStoreLinkClient(keys=FileStoreKeyProvider(str(keys_file)))
    client.get_inventory(ST, SKU)
    client.rotate_key(ST, "key-week-B")

    with pytest.raises(KeyExpired):
        client.get_inventory(ST, SKU)
    # The whole story in order: rejected -> secrets re-read -> still rejected.
    assert [r["event"] for r in _key_events()][-3:] == [
        "storelink_key_rotation_retry", "storelink_keys_loaded", "storelink_key_expired"
    ]


def test_unreadable_secrets_file_is_logged_with_what_survived(keys_file):
    provider = FileStoreKeyProvider(str(keys_file))
    keys_file.write_text("{ truncated")
    provider.refresh()

    failure = [r for r in diagnostic_log.read() if r["event"] == "storelink_keys_reload_failed"][-1]
    assert failure["keys_retained"] == 1
    assert provider.get_key(ST) == "key-week-A"   # still serving the last good keys


def test_no_key_material_ever_reaches_the_log(keys_file):
    client = StubStoreLinkClient(keys=FileStoreKeyProvider(str(keys_file)))
    client.get_inventory(ST, SKU)
    client.rotate_key(ST, "key-week-B")
    with pytest.raises(KeyExpired):
        client.get_inventory(ST, SKU)

    written = diagnostic_log.path.read_text()
    assert "key-week-A" not in written and "key-week-B" not in written
    assert ST in written  # store ids, on the other hand, are the whole point
