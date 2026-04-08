"""Tests for FleetDataLayer — unified SQLite connection management."""

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from multifleet.data_layer import FleetDataLayer, get_data_layer, reset_data_layer


@pytest.fixture
def data_dir(tmp_path):
    """Provide a temp directory for database files."""
    return tmp_path / "fleet-data"


@pytest.fixture
def dl(data_dir):
    """Create a fresh FleetDataLayer for each test."""
    layer = FleetDataLayer(data_dir=str(data_dir))
    yield layer
    layer.close_all()


@pytest.fixture(autouse=True)
def reset_singleton():
    """Ensure singleton is clean between tests."""
    yield
    reset_data_layer()


# ── 1. Basic construction ──

def test_creates_data_directory(data_dir):
    """Data layer creates its directory on init."""
    dl = FleetDataLayer(data_dir=str(data_dir))
    assert data_dir.exists()
    dl.close_all()


# ── 2. Connection creation with WAL ──

def test_connection_creates_db_with_wal(dl, data_dir):
    """First connection to a db creates the file with WAL mode."""
    with dl.connection("messages") as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()
        assert mode[0] == "wal"
    assert (data_dir / "messages.db").exists()


# ── 3. Connection reuse ──

def test_connection_reuse(dl):
    """Same db_name returns the same connection object."""
    with dl.connection("messages") as conn1:
        pass
    with dl.connection("messages") as conn2:
        pass
    assert conn1 is conn2


# ── 4. Multiple databases ──

def test_multiple_databases(dl, data_dir):
    """Different db_names create separate database files."""
    with dl.connection("messages") as conn_m:
        conn_m.execute("CREATE TABLE t1 (id INTEGER)")
    with dl.connection("evidence") as conn_e:
        conn_e.execute("CREATE TABLE t2 (id INTEGER)")

    assert (data_dir / "messages.db").exists()
    assert (data_dir / "evidence.db").exists()
    assert "messages" in dl.active_connections
    assert "evidence" in dl.active_connections


# ── 5. Close single db ──

def test_close_db(dl):
    """close_db removes connection from pool."""
    with dl.connection("messages") as conn:
        conn.execute("CREATE TABLE t (id INTEGER)")
    dl.close_db("messages")
    assert "messages" not in dl.active_connections


# ── 6. Close all ──

def test_close_all(dl):
    """close_all closes everything and marks layer as closed."""
    with dl.connection("messages"):
        pass
    with dl.connection("evidence"):
        pass
    dl.close_all()
    assert dl.active_connections == []
    with pytest.raises(RuntimeError, match="closed"):
        with dl.connection("messages"):
            pass


# ── 7. Health check ──

def test_health_check_no_dbs(dl):
    """Health check reports missing databases as unhealthy."""
    result = dl.health_check()
    assert not result["healthy"]
    assert "messages" in result["databases"]
    assert not result["databases"]["messages"]["exists"]


def test_health_check_with_dbs(dl):
    """Health check reports connected databases as healthy."""
    with dl.connection("messages"):
        pass
    with dl.connection("evidence"):
        pass
    result = dl.health_check()
    assert result["healthy"]
    assert result["databases"]["messages"]["wal_mode"]
    assert result["databases"]["evidence"]["connected"]


# ── 8. Maintenance ──

def test_run_maintenance(dl):
    """Maintenance runs checkpoint on open databases."""
    with dl.connection("messages") as conn:
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
    result = dl.run_maintenance()
    assert "messages" in result
    assert result["messages"]["integrity"] == "ok"


# ── 9. Thread safety ──

def test_thread_safe_connection(dl):
    """Multiple threads can safely get connections."""
    results = []
    errors = []

    def worker():
        try:
            with dl.connection("messages") as conn:
                conn.execute("SELECT 1")
                results.append(True)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 10
    assert len(errors) == 0


# ── 10. Singleton ──

def test_singleton(tmp_path):
    """get_data_layer returns the same instance."""
    d = str(tmp_path / "singleton-test")
    dl1 = get_data_layer(data_dir=d)
    dl2 = get_data_layer()
    assert dl1 is dl2
    reset_data_layer()


# ── 11. Custom db name ──

def test_custom_db_name(dl, data_dir):
    """Unknown db names get .db suffix automatically."""
    with dl.connection("custom") as conn:
        conn.execute("CREATE TABLE t (id INTEGER)")
    assert (data_dir / "custom.db").exists()


# ── 12. Store integration ──

def test_store_uses_data_layer(dl):
    """FleetNerveStore works with data_layer parameter."""
    from multifleet.store import FleetNerveStore
    store = FleetNerveStore(data_layer=dl)
    # Should use data layer's connection
    conn = store._conn()
    assert conn is dl.get_connection("messages")
    # Store operations should work
    store.store_message({
        "id": "test-1",
        "type": "context",
        "from": "mac1",
        "to": "mac2",
        "payload": {"subject": "test", "body": "hello"},
    })
    msg = store.get_message("test-1")
    assert msg is not None
    assert msg["from_node"] == "mac1"


# ── 13. Evidence ledger integration ──

def test_evidence_ledger_uses_data_layer(dl):
    """EvidenceLedger works with data_layer parameter."""
    from multifleet.evidence_ledger import EvidenceLedger
    ledger = EvidenceLedger(data_layer=dl)
    entry = ledger.record(
        event_type="test_event",
        node_id="mac1",
        subject="test",
        payload={"key": "value"},
    )
    assert entry["entry_id"]
    assert entry["entry_hash"]
    # Verify chain
    chain = ledger.verify_chain()
    assert chain["valid"]
    assert chain["checked"] == 1


# ── 14. Lease manager integration ──

def test_lease_manager_uses_data_layer(dl):
    """LeaseManager works with data_layer parameter."""
    from multifleet.store import FleetNerveStore
    from multifleet.leases import LeaseManager
    store = FleetNerveStore(data_layer=dl)
    lm = LeaseManager(store, node_id="mac1", data_layer=dl)
    result = lm.acquire("test-resource", lease_type="short_task")
    assert result["acquired"]
    assert result["holder"] == "mac1"
    # Query
    lease = lm.query("test-resource")
    assert lease is not None
    assert lease["active"]


# ── 15. get_connection raises after close ──

def test_get_connection_after_close(dl):
    """get_connection raises RuntimeError after close_all."""
    dl.close_all()
    with pytest.raises(RuntimeError, match="closed"):
        dl.get_connection("messages")
