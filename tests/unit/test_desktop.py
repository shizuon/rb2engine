"""Actual desktop conversions on authored fixtures, including Windows-sensitive paths."""
from __future__ import annotations

import hashlib
import sqlite3
import threading
from pathlib import Path

import pytest

from rb2engine.desktop.app import render_html
from rb2engine.desktop.service import DesktopService, discover_drives
from rb2engine.errors import FatalError
from rb2engine.writer import build as writer
from tests.desktop_fixture import check_analysis, make_drive


def wait(service: DesktopService) -> dict:
    assert service._thread is not None
    service._thread.join(timeout=20)
    state = service.get_state()
    assert not state["busy"], state
    return state


def manifest(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for parent in (root / "PIONEER", root / "Contents")
            for p in parent.rglob("*") if p.is_file()}


@pytest.fixture
def drive(tmp_path: Path) -> Path:
    # Spaces, non-ASCII, URI fragment and percent characters are legal on Windows.
    return make_drive(tmp_path / "演出 USB #1 100%")


def test_roundtrip_and_existing_database_backup(drive: Path) -> None:
    service = DesktopService()
    original = manifest(drive)
    assert service.inspect_drive(str(drive))["ok"]
    state = wait(service)
    assert state["status"] == "ready", state
    assert state["summary"] == {"tracks": 1, "playlists": 1, "missing": 0, "existing": False}
    assert not (drive / "Engine Library").exists()
    assert service.convert_drive(True, True, False)["ok"]
    state = wait(service)
    assert state["status"] in ("complete", "warning"), state
    assert state["result"]["counters"]["tracks_converted"] == 1
    assert service.verify_drive()["ok"]
    state = wait(service)
    assert state["status"] == "verified", state
    assert service.convert_drive(True, True, False)["error"] == "confirm_replace"
    assert service.convert_drive(True, True, True)["ok"]
    assert wait(service)["status"] in ("complete", "warning")
    backups = list((drive / "Engine Library" / "Database2" / "rb2engine-backups").glob("*.db"))
    assert len(backups) == 1
    with sqlite3.connect(str(backups[0])) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert conn.execute("SELECT count(*) FROM Track").fetchone() == (1,)
    assert manifest(drive) == original


def test_confirmation_and_failed_inspection_invalidate_target(drive: Path) -> None:
    service = DesktopService()
    assert service.convert_drive(True, True, False)["error"] == "inspect_first"
    assert service.verify_drive()["error"] == "inspect_first"
    service.inspect_drive(str(drive))
    wait(service)
    assert service.convert_drive(True, False, False)["error"] == "close_apps"
    service.inspect_drive(str(drive / "not a drive"))
    state = wait(service)
    assert state["status"] == "error"
    assert state["summary"] is None
    assert service.convert_drive(True, True, True)["error"] == "inspect_first"
    assert not (drive / "Engine Library").exists()


def test_optional_analysis_and_pickup_reach_written_database(tmp_path: Path) -> None:
    drive = make_drive(tmp_path / "pickup export", analysis=True)
    before = manifest(drive)
    service = DesktopService()
    assert service.inspect_drive(str(drive))["ok"]
    assert wait(service)["status"] == "ready"
    assert service.convert_drive(False, True, False)["ok"]
    assert wait(service)["status"] in ("complete", "warning")
    assert check_analysis(drive)["downbeat_seconds"] == 1.25
    assert service.verify_drive()["ok"]
    assert wait(service)["status"] == "verified"
    assert manifest(drive) == before


def test_export_change_requires_reinspection(drive: Path) -> None:
    service = DesktopService()
    service.inspect_drive(str(drive))
    wait(service)
    export = drive / "PIONEER" / "rekordbox" / "export.pdb"
    content = bytearray(export.read_bytes())
    content[4095] ^= 1  # valid unused header padding, changes the fingerprint
    export.write_bytes(content)
    service.convert_drive(False, True, False)
    state = wait(service)
    assert state["status"] == "error"
    assert "changed" in state["error"]
    assert state["summary"] is None
    assert not (drive / "Engine Library").exists()


def test_new_engine_database_requires_confirmation(drive: Path) -> None:
    service = DesktopService()
    service.inspect_drive(str(drive))
    wait(service)
    database = drive / "Engine Library" / "Database2" / "m.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"must not replace")
    service.convert_drive(True, True, False)
    state = wait(service)
    assert state["status"] == "error"
    assert database.read_bytes() == b"must not replace"


def test_busy_guard_and_close_protection() -> None:
    service = DesktopService()
    release = threading.Event()
    assert service._start("test", lambda: release.wait(10))["ok"]
    try:
        assert not service._can_close()
        assert service.get_state()["close_blocked"]
        assert service.inspect_drive("unused")["error"] == "busy"
        # Snapshots must not let JavaScript mutate backend state.
        service.get_state()["busy"] = False
        assert service.get_state()["busy"]
    finally:
        release.set()
        wait(service)
    assert service._can_close()


def test_backup_failure_never_replaces_database(
    drive: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = DesktopService()
    service.inspect_drive(str(drive))
    wait(service)
    service.convert_drive(False, True, False)
    state = wait(service)
    assert state["status"] in ("complete", "warning"), state
    database = drive / "Engine Library" / "Database2" / "m.db"
    original = database.read_bytes()

    def fail_backup(db2: Path) -> None:
        raise FatalError("Disk full during backup")

    monkeypatch.setattr(writer, "_backup_database", fail_backup)
    service.convert_drive(False, True, True)
    state = wait(service)
    assert state["status"] == "error"
    assert "Disk full" in state["error"]
    assert database.read_bytes() == original


def test_no_console_renderer_assets_and_drives() -> None:
    html = render_html()
    assert "/* APP_CSS */" not in html and "/* APP_JS */" not in html
    assert "pywebviewready" in html and 'id="convert"' in html
    assert "https://" not in html
    assert all(Path(d["path"]).is_absolute() for d in discover_drives())
