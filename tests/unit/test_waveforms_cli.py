"""CLI contracts: optional installs, retained failures, and safe library selection.

DSP bytes and publication transactions have their own independent tests. These
tests replace that expensive backend while exercising real Click parsing and,
for prepare, real read-only SQLite selection and path validation.
"""

from __future__ import annotations

import builtins
import importlib
import json
import platform
import sqlite3
import subprocess
import sys
import types
import zlib
from contextlib import closing
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from rb2engine.cli import main
from rb2engine.waveforms_cli import waveforms


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def install_backend(monkeypatch, name: str, **functions):
    """Install an optional backend without requiring NumPy or a native compiler."""
    package = importlib.import_module("rb2engine.overviews")
    module = types.ModuleType(f"rb2engine.overviews.{name}")
    for field, function in functions.items():
        setattr(module, field, function)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(package, name, module, raising=False)
    return module


@pytest.fixture
def generator(monkeypatch):
    context = object()
    return install_backend(
        monkeypatch,
        "generator",
        create_context=Mock(return_value=context),
        generate=Mock(return_value={"cache_path": "/generated/entry", "cache_hit": False}),
        verify_entry=Mock(return_value={"cache_key": "verified"}),
    )


@pytest.fixture
def engine(tmp_path):
    """A hand-authored schema sufficient to select existing Engine tracks."""
    drive = tmp_path.resolve() / "drive"
    database = drive / "Engine Library" / "Database2" / "m.db"
    database.parent.mkdir(parents=True)
    audio = drive / "Contents" / "fixture.wav"
    audio.parent.mkdir()
    audio.write_bytes(b"read-only audio fixture")
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.executescript("""
            CREATE TABLE Information(uuid TEXT, schemaVersionMajor INTEGER,
                schemaVersionMinor INTEGER, schemaVersionPatch INTEGER);
            INSERT INTO Information VALUES('f2581e47-ec76-4792-a3b4-6a501c249ab5', 3, 0, 2);
            CREATE TABLE Track(id INTEGER PRIMARY KEY, path TEXT);
            INSERT INTO Track VALUES(1, '../Contents/fixture.wav');
        """)
    return drive, database, audio


@pytest.fixture
def publication(monkeypatch, tmp_path):
    pytest.importorskip("numpy")
    module = importlib.import_module("rb2engine.overviews.publication")
    monkeypatch.setattr(module, "prepare", Mock(return_value={"tracks": 1}))
    # These tests stub generation to inspect CLI selection/report semantics.
    # Cache discovery must not invoke the native backend's platform guard or
    # touch the user's cache. The real Windows guard is tested independently.
    build = importlib.import_module("rb2engine.overviews._build")
    monkeypatch.setattr(build, "build_cache", lambda: tmp_path / "build")
    return module


def invoke_prepare(runner, drive, out, *arguments):
    return runner.invoke(waveforms, ["prepare", str(drive), "--out", str(out), *arguments])


def test_main_registers_waveforms_without_backend_imports(runner, monkeypatch):
    original = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "numpy" or name.startswith("rb2engine.overviews."):
            raise ImportError("optional backend blocked for test")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    for arguments in (["--help"], ["waveforms", "--help"], ["waveforms", "prepare", "--help"]):
        result = runner.invoke(main, arguments)
        assert result.exit_code == 0, result.output
    assert "waveforms" in runner.invoke(main, ["--help"]).output


@pytest.mark.parametrize(
    "command",
    ["doctor", "generate", "batch", "verify-cache", "prepare", "plan", "apply", "check", "restore"],
)
def test_every_waveform_command_has_help_without_backend(runner, command):
    result = runner.invoke(waveforms, [command, "--help"])
    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output


def test_missing_optional_dependency_has_install_guidance(runner, monkeypatch, tmp_path):
    audio = tmp_path / "song.wav"
    audio.touch()
    original = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "rb2engine.overviews.generator":
            raise ModuleNotFoundError("No module named 'numpy'")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    result = runner.invoke(waveforms, ["generate", str(audio)])
    assert result.exit_code != 0
    assert "rb2engine[waveforms]" in result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize("no_aac", [False, True])
def test_doctor_routes_aac_choice(runner, monkeypatch, no_aac):
    backend = install_backend(monkeypatch, "doctor", doctor=Mock(return_value={"ready": True}))
    result = runner.invoke(waveforms, ["doctor", *(["--no-aac"] if no_aac else [])])
    assert result.exit_code == 0, result.output
    backend.doctor.assert_called_once_with(include_aac=not no_aac)
    assert json.loads(result.stdout)["ready"]


def test_generate_explicit_cache_is_canonical_and_audio_is_read_only(runner, generator, tmp_path):
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"audio must survive")
    cache = tmp_path / "nested" / ".." / "cache"
    result = runner.invoke(waveforms, ["generate", str(audio), "--cache", str(cache)])
    assert result.exit_code == 0, result.output
    generator.generate.assert_called_once_with(audio, cache.resolve())
    assert audio.read_bytes() == b"audio must survive"
    assert json.loads(result.stdout)["cache_path"] == "/generated/entry"


def test_generate_default_cache_is_sibling_of_native_build_cache(
    runner, generator, monkeypatch, tmp_path
):
    audio = tmp_path / "song.wav"
    audio.touch()
    build = tmp_path / "user-cache" / "build"
    install_backend(monkeypatch, "_build", build_cache=Mock(return_value=build))
    result = runner.invoke(waveforms, ["generate", str(audio)])
    assert result.exit_code == 0, result.output
    generator.generate.assert_called_once_with(audio, build.parent / "waveforms")


def test_windows_source_requires_bundled_runtime(runner, monkeypatch, tmp_path):
    pytest.importorskip("numpy")
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"guard must run before audio probing")
    cache = tmp_path / "cache"
    result = runner.invoke(waveforms, ["generate", str(audio), "--cache", str(cache)])
    assert result.exit_code != 0
    assert "Use the desktop waveform runtime on Windows" in result.output
    assert not cache.exists()


def test_verify_cache_reports_backend_verification(runner, generator, tmp_path):
    result = runner.invoke(waveforms, ["verify-cache", str(tmp_path)])
    assert result.exit_code == 0, result.output
    generator.verify_entry.assert_called_once_with(tmp_path)
    assert json.loads(result.stdout) == {"cache_key": "verified"}


def test_batch_retains_failed_song_and_uses_one_runtime(runner, generator, tmp_path):
    manifest = tmp_path / "songs.json"
    manifest.write_text(json.dumps(["first.wav", "bad.wav", "last.wav"]))
    report = tmp_path / "report.json"
    cache = tmp_path / "cache"
    generator.generate.side_effect = [
        {"cache_path": "first-entry"},
        ValueError("Unsupported test codec"),
        {"cache_path": "last-entry"},
    ]
    result = runner.invoke(
        waveforms, ["batch", str(manifest), "--report", str(report), "--cache", str(cache)]
    )
    assert result.exit_code == 1, result.output
    data = json.loads(report.read_text())
    assert [row["status"] for row in data["samples"]] == ["generated", "error", "generated"]
    assert data["samples"][1]["audio"] == "bad.wav"
    assert "Unsupported test codec" in data["samples"][1]["error"]
    assert data["failures"] == 1
    assert data["database_written"] is False
    generator.create_context.assert_called_once()
    context = generator.create_context.return_value
    assert all(call.kwargs["context"] is context for call in generator.generate.call_args_list)
    assert json.loads(result.stdout)["failures"] == 1


@pytest.mark.parametrize("manifest_value", [[], {}, [1], "song.wav"])
def test_invalid_batch_manifest_does_not_start_generation(
    runner, generator, tmp_path, manifest_value
):
    manifest = tmp_path / "songs.json"
    manifest.write_text(json.dumps(manifest_value))
    report = tmp_path / "report.json"
    result = runner.invoke(waveforms, ["batch", str(manifest), "--report", str(report)])
    assert result.exit_code != 0
    generator.generate.assert_not_called()
    generator.create_context.assert_not_called()
    assert not report.exists()


def test_batch_refuses_report_collision_before_generation(runner, generator, tmp_path):
    manifest = tmp_path / "songs.json"
    manifest.write_text('["song.wav"]')
    report = tmp_path / "existing.json"
    report.write_text("keep original report")
    result = runner.invoke(waveforms, ["batch", str(manifest), "--report", str(report)])
    assert result.exit_code != 0
    assert report.read_text() == "keep original report"
    generator.create_context.assert_not_called()


@pytest.mark.parametrize("allow_partial", [False, True])
def test_prepare_retains_missing_audio_and_requires_explicit_partial(
    runner, engine, publication, monkeypatch, allow_partial
):
    drive, database, audio = engine
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("INSERT INTO Track VALUES(2, '../Contents/missing.wav')")
    before = database.read_bytes()
    backend = install_backend(
        monkeypatch,
        "generator",
        create_context=Mock(return_value=object()),
        generate=Mock(side_effect=[{"cache_path": "good-cache"}, FileNotFoundError("missing.wav")]),
    )
    out = drive / "Engine Library" / "batch"
    result = invoke_prepare(runner, drive, out, *(["--allow-partial"] if allow_partial else []))
    assert result.exit_code == 1, result.output
    report = json.loads((out / "generation.json").read_text())
    assert report["failures"] == 1
    assert [row["track_id"] for row in report["samples"]] == [1, 2]
    assert [row["status"] for row in report["samples"]] == ["generated", "error"]
    assert backend.generate.call_count >= 1
    if allow_partial:
        publication.prepare.assert_called_once_with(
            database, [{"track_id": 1, "cache_path": "good-cache"}], out / "publication"
        )
    else:
        publication.prepare.assert_not_called()
    assert json.loads(result.stdout)["plan_created"] is allow_partial
    assert database.read_bytes() == before
    assert audio.read_bytes() == b"read-only audio fixture"


def test_prepare_selected_track_and_report_stay_inside_library(
    runner, engine, publication, monkeypatch
):
    drive, database, audio = engine
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("INSERT INTO Track VALUES(2, '../Contents/fixture.wav')")
    before = database.read_bytes()
    selected = []

    def generate_rows(items, cache):
        selected.extend(items)
        return [
            {**item, "status": "generated", "result": {"cache_path": "good-cache"}}
            for item in items
        ]

    monkeypatch.setattr("rb2engine.waveforms_cli._generate_many", generate_rows)
    out = drive / "Engine Library" / "batch"
    result = invoke_prepare(runner, drive, out, "--track", "2")
    assert result.exit_code == 0, result.output
    assert selected == [{"track_id": 2, "audio": str(audio)}]
    publication.prepare.assert_called_once_with(
        database, [{"track_id": 2, "cache_path": "good-cache"}], out / "publication"
    )
    assert (out / "generation.json").is_file()
    assert database.read_bytes() == before
    assert [
        path.relative_to(drive).as_posix()
        for path in drive.rglob("*")
        if path.is_file() and not path.is_relative_to(drive / "Engine Library")
    ] == ["Contents/fixture.wav"]


def test_prepare_default_batch_is_new_and_belongs_to_library(
    runner, engine, publication, monkeypatch
):
    drive, database, _ = engine
    monkeypatch.setattr(
        "rb2engine.waveforms_cli._generate_many",
        Mock(
            return_value=[{"track_id": 1, "status": "generated", "result": {"cache_path": "entry"}}]
        ),
    )
    result = runner.invoke(waveforms, ["prepare", str(drive)])
    assert result.exit_code == 0, result.output
    batch = json.loads(result.stdout)["batch"]
    from pathlib import Path

    path = Path(batch)
    assert path.parent == drive / "Engine Library" / "rb2engine-waveforms"
    assert (path / "generation.json").is_file()
    publication.prepare.assert_called_once_with(
        database, [{"track_id": 1, "cache_path": "entry"}], path / "publication"
    )


def test_all_failed_tracks_never_produce_a_plan_even_with_allow_partial(
    runner, engine, publication, monkeypatch
):
    drive, _, _ = engine
    monkeypatch.setattr(
        "rb2engine.waveforms_cli._generate_many",
        Mock(return_value=[{"track_id": 1, "status": "error", "error": "unsupported codec"}]),
    )
    out = drive / "Engine Library" / "batch"
    result = invoke_prepare(runner, drive, out, "--allow-partial")
    assert result.exit_code == 1, result.output
    assert json.loads((out / "generation.json").read_text())["failures"] == 1
    assert json.loads(result.stdout)["plan_created"] is False
    publication.prepare.assert_not_called()


def test_empty_library_does_not_start_generation_or_create_batch(
    runner, engine, publication, monkeypatch
):
    drive, database, _ = engine
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("DELETE FROM Track")
    generate_many = Mock()
    monkeypatch.setattr("rb2engine.waveforms_cli._generate_many", generate_many)
    out = drive / "Engine Library" / "batch"
    result = invoke_prepare(runner, drive, out)
    assert result.exit_code != 0
    assert "No tracks selected" in result.output
    assert not out.exists()
    generate_many.assert_not_called()


@pytest.mark.parametrize(
    "invalid", ["unknown-track", "unsupported-schema", "outside-output", "existing-output"]
)
def test_prepare_rejects_invalid_selection_before_generation(
    runner, engine, publication, monkeypatch, tmp_path, invalid
):
    drive, database, _ = engine
    out = drive / "Engine Library" / "batch"
    arguments = []
    if invalid == "unknown-track":
        arguments = ["--track", "9"]
    elif invalid == "unsupported-schema":
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("UPDATE Information SET schemaVersionPatch=99")
    elif invalid == "outside-output":
        out = tmp_path / "outside"
    else:
        out.mkdir()
        (out / "keep.txt").write_text("original")
    before = database.read_bytes()
    generate_many = Mock()
    monkeypatch.setattr("rb2engine.waveforms_cli._generate_many", generate_many)
    result = invoke_prepare(runner, drive, out, *arguments)
    assert result.exit_code != 0
    generate_many.assert_not_called()
    publication.prepare.assert_not_called()
    assert database.read_bytes() == before
    assert not (out / "generation.json").exists()


@pytest.mark.parametrize("link_kind", ["drive", "database", "output-parent"])
def test_prepare_rejects_symlink_before_generation(
    runner, engine, publication, monkeypatch, tmp_path, link_kind
):
    drive, database, _ = engine
    out = drive / "Engine Library" / "batch"
    try:
        if link_kind == "drive":
            alias = tmp_path / "drive-link"
            alias.symlink_to(drive, target_is_directory=True)
            drive = alias
        elif link_kind == "database":
            original = database.with_name("original.db")
            database.rename(original)
            database.symlink_to(original)
        else:
            alias = drive / "Engine Library" / "alias"
            alias.symlink_to(drive / "Engine Library", target_is_directory=True)
            out = alias / "batch"
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks unavailable on this platform")
    generate_many = Mock(return_value=[])
    monkeypatch.setattr("rb2engine.waveforms_cli._generate_many", generate_many)
    result = invoke_prepare(runner, drive, out)
    assert result.exit_code != 0, result.output
    assert "symlink" in result.output.lower()
    generate_many.assert_not_called()
    publication.prepare.assert_not_called()
    assert not (out / "generation.json").exists()


@pytest.mark.parametrize(
    "command,method", [("apply", "apply"), ("check", "verify"), ("restore", "restore")]
)
def test_publication_actions_route_exact_folder(runner, monkeypatch, tmp_path, command, method):
    methods = {name: Mock(return_value={"state": name}) for name in ("apply", "verify", "restore")}
    install_backend(monkeypatch, "publication", **methods)
    result = runner.invoke(waveforms, [command, str(tmp_path)])
    assert result.exit_code == 0, result.output
    methods[method].assert_called_once_with(tmp_path)
    assert json.loads(result.stdout) == {"state": method}
    assert all(mock.call_count == 0 for name, mock in methods.items() if name != method)


@pytest.mark.parametrize(
    "error",
    [
        ValueError("Changed plan"),
        RuntimeError("Engine running"),
        sqlite3.OperationalError("database is locked"),
        subprocess.CalledProcessError(1, "xcrun"),
        zlib.error("incorrect header check"),
    ],
)
def test_publication_failures_return_clear_nonzero_exit(runner, monkeypatch, tmp_path, error):
    install_backend(
        monkeypatch, "publication", apply=Mock(side_effect=error), verify=Mock(), restore=Mock()
    )
    result = runner.invoke(waveforms, ["apply", str(tmp_path)])
    assert result.exit_code != 0
    assert str(error) in result.output
    assert "Traceback" not in result.output


def test_explicit_plan_routes_mapping_without_rewriting_database(runner, monkeypatch, tmp_path):
    database = tmp_path / "m.db"
    database.write_bytes(b"unchanged database")
    manifest = tmp_path / "mapping.json"
    entries = [{"track_id": 4, "cache_path": "cache-entry"}]
    manifest.write_text(json.dumps(entries))
    out = tmp_path / "plan"
    prepare = Mock(return_value={"tracks": 1})
    install_backend(monkeypatch, "publication", prepare=prepare)
    result = runner.invoke(waveforms, ["plan", str(database), str(manifest), "--out", str(out)])
    assert result.exit_code == 0, result.output
    prepare.assert_called_once_with(database, entries, out)
    assert database.read_bytes() == b"unchanged database"
