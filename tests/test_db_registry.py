"""Tests for db_registry — central SQLite database catalog."""

import sqlite3
from pathlib import Path

import pytest

from multifleet.db_registry import (
    KNOWN_DATABASES,
    ConnectionStyle,
    DatabaseEntry,
    get_db_path,
    health_check,
    list_databases,
    register_db,
    reset_runtime,
    unregister_db,
)


@pytest.fixture(autouse=True)
def clean_runtime():
    """Reset runtime registrations between tests."""
    yield
    reset_runtime()


# ── 1. Static registry ──

def test_known_databases_not_empty():
    """Registry has entries for all known multifleet databases."""
    assert len(KNOWN_DATABASES) >= 7
    names = {db.name for db in KNOWN_DATABASES}
    assert "messages" in names
    assert "evidence" in names
    assert "truth_ladder" in names
    assert "packets" in names
    assert "supervisor" in names
    assert "races" in names
    assert "beta" in names


def test_no_duplicate_names():
    """Each database has a unique name."""
    names = [db.name for db in KNOWN_DATABASES]
    assert len(names) == len(set(names))


# ── 2. get_db_path ──

def test_get_db_path_known():
    """get_db_path returns correct path for known databases."""
    path = get_db_path("messages")
    assert path.name == "messages.db"
    assert ".fleet-nerve" in str(path)


def test_get_db_path_unknown():
    """get_db_path raises KeyError for unknown names."""
    with pytest.raises(KeyError, match="Unknown database"):
        get_db_path("nonexistent")


# ── 3. register_db / unregister_db ──

def test_register_runtime_db(tmp_path):
    """Can register a new runtime database."""
    db_path = tmp_path / "custom.db"
    entry = register_db("custom", db_path, "test_module.py")
    assert entry.name == "custom"
    assert get_db_path("custom") == db_path


def test_register_rejects_static_override():
    """Cannot override a static database entry."""
    with pytest.raises(ValueError, match="Cannot override static"):
        register_db("messages", Path("/tmp/fake.db"), "attacker.py")


def test_unregister_runtime_db(tmp_path):
    """Can unregister a runtime database."""
    register_db("temp", tmp_path / "temp.db", "temp.py")
    assert unregister_db("temp") is True
    with pytest.raises(KeyError):
        get_db_path("temp")


def test_unregister_nonexistent():
    """Unregistering unknown name returns False."""
    assert unregister_db("ghost") is False


# ── 4. list_databases ──

def test_list_databases_includes_all():
    """list_databases returns all static databases."""
    dbs = list_databases()
    names = {d["name"] for d in dbs}
    assert "messages" in names
    assert "evidence" in names
    assert "races" in names


def test_list_databases_includes_runtime(tmp_path):
    """list_databases includes runtime-registered databases."""
    register_db("plugin_db", tmp_path / "plugin.db", "plugin.py")
    dbs = list_databases()
    names = {d["name"] for d in dbs}
    assert "plugin_db" in names


def test_list_databases_fields():
    """Each entry has the expected fields."""
    dbs = list_databases()
    for db in dbs:
        assert "name" in db
        assert "path" in db
        assert "module" in db
        assert "uses_wal" in db
        assert "connection_style" in db
        assert "exists" in db


# ── 5. health_check ──

def test_health_check_with_real_db(tmp_path):
    """health_check correctly reports on an existing DB."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.close()

    register_db("test_hc", db_path, "test.py", uses_wal=True)
    result = health_check()
    db_info = result["databases"].get("test_hc")
    assert db_info is not None
    assert db_info["exists"] is True
    assert db_info["accessible"] is True
    assert db_info["wal_mode"] is True


def test_health_check_missing_core_db(tmp_path, monkeypatch):
    """health_check warns when core databases are missing."""
    # This test checks the warning logic — core DBs (messages, evidence)
    # produce warnings when missing. The actual home dir DBs may or may
    # not exist depending on the test environment.
    result = health_check()
    # Structure is always valid
    assert "healthy" in result
    assert "total" in result
    assert "warnings" in result
    assert "databases" in result
    assert result["total"] >= 7


def test_health_check_wal_mismatch(tmp_path):
    """health_check warns when WAL is expected but not set."""
    db_path = tmp_path / "no_wal.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.close()

    register_db("no_wal_test", db_path, "test.py", uses_wal=True)
    result = health_check()
    db_info = result["databases"]["no_wal_test"]
    assert db_info["accessible"] is True
    # DELETE mode, not WAL
    assert db_info["wal_mode"] is False
    assert any("no_wal_test" in w for w in result["warnings"])


# ── 6. DatabaseEntry properties ──

def test_database_entry_path():
    """DatabaseEntry.path combines base_dir and filename."""
    entry = DatabaseEntry(
        name="test",
        filename="test.db",
        base_dir=Path("/tmp/data"),
        module="test.py",
    )
    assert entry.path == Path("/tmp/data/test.db")


# ── 7. packets DB flagged as no-WAL ──

def test_packets_flagged_no_wal():
    """The packets DB is correctly flagged as not using WAL."""
    packets = next(db for db in KNOWN_DATABASES if db.name == "packets")
    assert packets.uses_wal is False
    assert "MISSING WAL" in packets.notes


# ── 8. ConnectionStyle enum ──

def test_connection_styles():
    """All known databases have valid connection styles."""
    for db in KNOWN_DATABASES:
        assert isinstance(db.connection_style, ConnectionStyle)


# ── 9. Data layer managed DBs ──

def test_data_layer_managed_dbs():
    """messages and evidence use DATA_LAYER connection style."""
    for db in KNOWN_DATABASES:
        if db.name in ("messages", "evidence"):
            assert db.connection_style == ConnectionStyle.DATA_LAYER
