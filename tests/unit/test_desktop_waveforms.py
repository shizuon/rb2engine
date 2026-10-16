"""Desktop publication preserves sources, existing waveforms and concurrent edits."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from rb2engine.desktop import waveforms
from rb2engine.desktop.service import DesktopService
from rb2engine.overviews import generator
from rb2engine.overviews import publication as pub
from rb2engine.overviews.formats import pack
from tests.desktop_fixture import make_drive
from tests.unit.test_desktop import manifest, wait


@pytest.fixture
def converted(tmp_path, monkeypatch):
    from rb2engine.writer import build

    monkeypatch.setattr(build, "detect_desktop_schema", lambda: (3, 0, 2))
    drive = make_drive(tmp_path / "演出 USB #1 100%")
    service = DesktopService()
    service.inspect_drive(str(drive))
    wait(service)
    service.convert_drive(False, True, False)
    state = wait(service)
    assert state["status"] in ("complete", "warning")
    return drive, service, Path(state["result"]["database"])


def cached_silence(audio: Path, cache: Path) -> dict:
    runtime = {"test": "authored silence"}
    source = hashlib.sha256(audio.read_bytes()).hexdigest()
    identity = generator.cache_key(source, runtime, "fixture")
    folder = cache / identity
    folder.mkdir(parents=True)
    points = np.zeros((1024, 3), np.uint8)
    outputs = {}
    for kind in ("tri", "rgb"):
        blob = pack(points, 44100, 420, kind)
        (folder / f"overview.{kind}").write_bytes(blob)
        outputs[kind] = hashlib.sha256(blob).hexdigest()
    report = {"schema": 2, "cache_key": identity, "source_sha256": source,
              "runtime": runtime, "implementation_sha256": "fixture",
              "sample_rate": 44100, "declared_frames": 44100, "block_frames": 420,
              "outputs_sha256": outputs}
    (folder / "result.json").write_text(json.dumps(report), encoding="utf-8")
    return {**report, "cache_path": str(folder)}


def test_fill_existing_without_rebuild_and_retry_preserves_waveforms(
    converted, monkeypatch, tmp_path,
):
    drive, service, database = converted
    original = manifest(drive)
    entry = cached_silence(drive / "Contents/test.wav", tmp_path / "cache")
    monkeypatch.setattr(generator, "create_context", object)
    monkeypatch.setattr(generator, "generate", lambda *_a, **_kw: entry)
    assert service.fill_waveforms(False)["error"] == "close_apps"
    assert service.fill_waveforms(True)["ok"]
    state = wait(service)
    assert state["status"] == "waveforms_complete", state
    result = state["result"]["waveforms"]
    assert result["generated"] == 1 and result["verified"]["tri_rgb_readback_matches"]
    batch = Path(result["batch"])
    assert (batch / "before.sqlite3").exists()
    after = database.read_bytes()
    service.fill_waveforms(True)
    again = wait(service)["result"]["waveforms"]
    assert again["generated"] == 0 and again["preserved"] == 1
    assert database.read_bytes() == after
    assert manifest(drive) == original
    assert pub.restore(batch)["state"] == "restored"
    assert manifest(drive) == original


def test_decoder_failure_keeps_converted_library(converted, monkeypatch):
    drive, service, database = converted
    original_db, original = database.read_bytes(), manifest(drive)
    monkeypatch.setattr(generator, "create_context", object)

    def fail(*args, **kwargs):
        raise ValueError("Unsupported sample rate")

    monkeypatch.setattr(generator, "generate", fail)
    service.fill_waveforms(True)
    state = wait(service)
    assert state["status"] == "warning"
    assert state["result"]["waveforms"]["failures"][0]["track_id"] == 1
    assert database.read_bytes() == original_db and manifest(drive) == original
    assert not (database.parent.parent / "rb2engine-waveforms").exists()


@pytest.mark.parametrize("change", ["metadata", "rgb"])
def test_concurrent_change_during_generation_refuses_publication(
    converted, monkeypatch, tmp_path, change,
):
    drive, _service, database = converted
    entry = cached_silence(drive / "Contents/test.wav", tmp_path / "cache")

    def concurrent(*args, **kwargs):
        with sqlite3.connect(database) as connection:
            connection.row_factory = sqlite3.Row
            if change == "metadata":
                connection.execute("UPDATE Track SET title='Concurrent edit' WHERE id=1")
            else:
                uid = pub.schema(connection)
                rgb = pub.rgb_path(database, uid, 1)
                rgb.parent.mkdir(parents=True)
                rgb.write_bytes(b"concurrent waveform")
        return entry

    monkeypatch.setattr(generator, "generate", concurrent)
    with pytest.raises(ValueError, match=r"during.*generation"):
        waveforms.fill(database, lambda *_a: None, context=object())
    with sqlite3.connect(database) as connection:
        row = connection.execute("SELECT overviewWaveFormData FROM PerformanceData").fetchone()
        assert not row[0]


@pytest.mark.skipif(
    not os.environ.get("RB2ENGINE_NATIVE_RUNTIME"), reason="bundled runtime required"
)
def test_real_bundled_desktop_conversion_waveforms_verify_and_restore(tmp_path, monkeypatch):
    monkeypatch.setenv("RB2ENGINE_OVERVIEWS_BUILD_CACHE", str(tmp_path / "runtime"))
    drive = make_drive(tmp_path / "中文 USB #2 100%", tone=True)
    original = manifest(drive)
    service = DesktopService()
    service.inspect_drive(str(drive))
    wait(service)
    service.convert_drive(False, True, False, False, True)
    state = wait(service)
    result = state["result"]["waveforms"]
    assert result["generated"] == 1 and not result["failures"], state
    assert result["verified"]["all_protected_fields_unchanged"]
    service.verify_drive()
    assert wait(service)["status"] == "verified"
    assert pub.restore(Path(result["batch"]))["state"] == "restored"
    assert manifest(drive) == original
