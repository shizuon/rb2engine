"""Optional OpenKeyScan analyzer bridge. Reads audio; never writes file tags.

Protocol: https://github.com/rekordcloud/openkeyscan-analyzer
The desktop REST API respects auto-tagging settings, so this integration uses
its standalone analyzer's UTF-8 JSON-lines protocol instead.
"""
from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

from rb2engine.errors import FatalError
from rb2engine.ir import SourceLibrary
from rb2engine.mapper.keys import key_name_to_ordinal
from rb2engine.progress import ProgressCallback

DOWNLOAD_URL = "https://www.openkeyscan.com/download"
_NAMES = ("openkeyscan-analyzer.exe", "openkeyscan-analyzer", "openkeyscan_analyzer.exe")
_KEY = re.compile(r"(?:[1-9]|1[0-2])(?:[dm]|[AB])\Z", re.IGNORECASE)


def digest_record(record: dict[str, Any]) -> str:
    payload = json.dumps(record, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(payload).hexdigest()


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def resolve_analyzer(selected: str) -> Path:
    """Accept an analyzer binary, app bundle or installation directory."""
    root = Path(selected).expanduser()
    if not selected:
        raise FatalError(f"OpenKeyScan is not installed. Download: {DOWNLOAD_URL}")
    if root.is_file() and root.name.lower() in _NAMES:
        return root.resolve()
    if root.is_file() and root.name.lower() == "openkeyscan.exe":
        root = root.parent
    if root.is_dir():
        # Search only recognized app/resource roots, never an entire volume.
        candidates = [root / name for name in _NAMES]
        for resources in (root / "Contents/Resources", root / "resources"):
            candidates.extend(resources / "app.asar.unpacked/lib/openkeyscan-analyzer" / name
                              for name in _NAMES)
        for base in (root / "Contents" / "Resources", root / "resources",
                     root / "openkeyscan-analyzer"):
            if base.is_dir():
                for name in _NAMES:
                    candidates.extend(base.glob(f"*/{name}"))
                    candidates.extend(base.glob(f"*/*/{name}"))
                    candidates.append(base / name)
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()
    raise FatalError(
        "OpenKeyScan analyzer not found. Select OpenKeyScan.app, its installation folder, "
        f"or openkeyscan-analyzer.exe. Download: {DOWNLOAD_URL}"
    )


def _windows_install_roots() -> list[Path]:
    """Read installer registrations, including custom install locations."""
    if sys.platform != "win32":
        return []
    import winreg

    roots: list[Path] = []
    uninstall = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, uninstall, 0, winreg.KEY_READ | view) as parent:
                    for index in range(winreg.QueryInfoKey(parent)[0]):
                        try:
                            with winreg.OpenKey(parent, winreg.EnumKey(parent, index)) as key:
                                name = str(winreg.QueryValueEx(key, "DisplayName")[0])
                                if name.casefold() == "openkeyscan":
                                    location = winreg.QueryValueEx(key, "InstallLocation")[0]
                                    if isinstance(location, str) and location:
                                        roots.append(Path(location))
                        except OSError:
                            continue
            except OSError:
                continue
    return roots


def _windows_msix_roots() -> list[Path]:
    """The official Windows installer currently installs an MSIX package."""
    if sys.platform != "win32":
        return []
    system = os.environ.get("SYSTEMROOT")
    if not system:
        return []
    powershell = Path(system) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    script = (
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
        "Get-AppxPackage -Name Openkeyscan | Select-Object -ExpandProperty InstallLocation"
    )
    process = None
    try:
        process = _spawn([str(powershell), "-NoProfile", "-NonInteractive", "-Command", script],
                         subprocess.DEVNULL)
        output, _ = process.communicate(timeout=10)
        return [Path(line.strip()) for line in output.splitlines() if line.strip()]
    except (OSError, subprocess.TimeoutExpired):
        if process is not None:
            process.kill()
            process.communicate()
        return []


def find_installed_analyzer() -> str:
    roots = [Path("/Applications/OpenKeyScan.app"), Path.home() / "Applications/OpenKeyScan.app"]
    if sys.platform == "win32":
        roots = [Path(value) / suffix for key, suffix in (
            ("LOCALAPPDATA", "Programs/OpenKeyScan"), ("LOCALAPPDATA", "OpenKeyScan"),
            ("PROGRAMFILES", "OpenKeyScan"), ("PROGRAMFILES(X86)", "OpenKeyScan"),
        ) if (value := os.environ.get(key))]
        roots.extend(_windows_install_roots())
        roots.extend(_windows_msix_roots())
    for root in roots:
        try:
            return str(resolve_analyzer(str(root)))
        except FatalError:
            continue
    return ""


def config_path() -> Path:
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    elif sys.platform == "darwin":
        root = Path.home() / "Library/Application Support"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return root / "rb2engine/openkeyscan.json"


def saved_analyzer() -> str:
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
        path = data.get("path") if isinstance(data, dict) else None
        return str(resolve_analyzer(path)) if isinstance(path, str) else ""
    except (OSError, ValueError, FatalError):
        return ""


def save_analyzer(path: str) -> None:
    target = config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps({"path": path}, ensure_ascii=False), encoding="utf-8")
    temporary.replace(target)


def _spawn(command: Sequence[str], stderr: Any) -> subprocess.Popen[str]:
    env = os.environ.copy()
    # A frozen app must not lend its Python/DLL paths to another frozen app.
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    for name in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
        if f"{name}_ORIG" in env:
            env[name] = env[f"{name}_ORIG"]
        else:
            env.pop(name, None)
    kwargs: dict[str, Any] = {
        "stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": stderr,
        "text": True, "encoding": "utf-8", "errors": "replace", "bufsize": 1,
        "cwd": str(Path(command[0]).parent), "env": env,
    }
    dll_directory = None
    kernel = None
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        if getattr(sys, "frozen", False):
            import ctypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetDllDirectoryW.argtypes = [ctypes.c_uint, ctypes.c_wchar_p]
            kernel.GetDllDirectoryW.restype = ctypes.c_uint
            kernel.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
            kernel.SetDllDirectoryW.restype = ctypes.c_int
            buffer = ctypes.create_unicode_buffer(32768)
            kernel.GetDllDirectoryW(len(buffer), buffer)
            dll_directory = buffer.value
            kernel.SetDllDirectoryW(None)
    try:
        return subprocess.Popen(list(command), **kwargs)  # noqa: S603 - user-selected local analyzer
    finally:
        if kernel is not None:
            kernel.SetDllDirectoryW(dll_directory)


class Analyzer:
    """One long-lived model process per batch, bounded waits and strict replies."""

    def __init__(self, command: Sequence[str], *, timeout: float = 180) -> None:
        self.timeout = timeout
        self._messages: queue.Queue[str | None] = queue.Queue()
        self._stderr = tempfile.TemporaryFile()  # noqa: SIM115 - closed with the process
        self._reader: threading.Thread | None = None
        self._process: subprocess.Popen[str] | None = None
        try:
            self._process = _spawn(command, self._stderr)
            assert self._process.stdout is not None
            self._reader = threading.Thread(target=self._read, args=(self._process.stdout,),
                                            daemon=True)
            self._reader.start()
            if self._receive().get("type") != "ready":
                raise FatalError("OpenKeyScan returned an incompatible startup response")
        except (OSError, FatalError) as exc:
            self.close()
            raise FatalError(f"OpenKeyScan could not start: {exc}") from exc

    def _read(self, stream: TextIO) -> None:
        try:
            while line := stream.readline(65537):
                self._messages.put(line)
                if len(line) > 65536:
                    break
        finally:
            self._messages.put(None)

    def _receive(self) -> dict[str, Any]:
        try:
            line = self._messages.get(timeout=self.timeout)
        except queue.Empty as exc:
            raise FatalError("OpenKeyScan timed out. Check the analyzer and try again.") from exc
        if line is None:
            self._stderr.seek(0, os.SEEK_END)
            end = self._stderr.tell()
            self._stderr.seek(max(0, end - 2000))
            detail = self._stderr.read().decode("utf-8", errors="replace").strip()
            raise FatalError(f"OpenKeyScan exited without a result. {detail}")
        try:
            reply = json.loads(line)
        except ValueError as exc:
            raise FatalError("OpenKeyScan returned invalid JSON") from exc
        if not isinstance(reply, dict) or len(line) > 65536:
            raise FatalError("OpenKeyScan returned an invalid response")
        return reply

    def analyze(self, path: Path, request_id: str) -> str:
        assert self._process is not None and self._process.stdin is not None
        try:
            self._process.stdin.write(json.dumps({"id": request_id, "path": str(path)},
                                                ensure_ascii=False) + "\n")
            self._process.stdin.flush()
        except OSError as exc:
            raise FatalError("OpenKeyScan disconnected") from exc
        reply = self._receive()
        if reply.get("id") != request_id:
            raise FatalError("OpenKeyScan response belongs to a different track")
        if reply.get("status") != "success":
            raise ValueError(str(reply.get("error", "Analysis failed")))
        key = reply.get("openkey") or reply.get("camelot")
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise ValueError("OpenKeyScan returned an invalid key")
        ordinal = key_name_to_ordinal(key)
        other = reply.get("camelot")
        if other is not None and (
            not isinstance(other, str) or not _KEY.fullmatch(other)
            or key_name_to_ordinal(other) != ordinal
        ):
            raise ValueError("OpenKeyScan returned inconsistent key notations")
        return key

    def close(self) -> None:
        if self._process is not None:
            process = self._process
            if process.stdin is not None:
                with contextlib.suppress(OSError):
                    process.stdin.close()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            if self._reader is not None:
                self._reader.join(timeout=2)
            if process.stdout is not None:
                process.stdout.close()
            self._process = None
        self._stderr.close()


def fill_missing_keys(
    library: SourceLibrary, executable: Path, *, on_progress: ProgressCallback | None = None,
) -> tuple[SourceLibrary, dict[str, Any]]:
    missing = [t for t in library.tracks.values() if not (t.key_name or "").strip()]
    record: dict[str, Any] = {
        "provider": "OpenKeyScan", "analyzer_sha256": file_hash(executable),
        "tracks": [], "failures": [],
    }
    if not missing:
        return library, record
    if on_progress:
        on_progress("starting key analysis", 0, 0)
    analyzer = Analyzer([str(executable), "--device", "cpu", "-w", "1"])
    tracks = dict(library.tracks)
    try:
        for done, track in enumerate(missing, 1):
            if on_progress:
                on_progress("analyzing keys", done - 1, len(missing))
            try:
                if track.resolved_path is None:
                    raise ValueError("Audio file is missing")
                before = file_hash(track.resolved_path)
                key = analyzer.analyze(track.resolved_path, str(track.rb_id))
                if file_hash(track.resolved_path) != before:
                    raise FatalError("Audio changed during key analysis; conversion stopped")
                tracks[track.rb_id] = dataclasses.replace(track, key_name=key)
                record["tracks"].append({
                    "track_id": track.rb_id, "key": key, "audio_sha256": before,
                })
            except (OSError, ValueError) as exc:
                record["failures"].append({"track_id": track.rb_id, "error": str(exc)})
            if on_progress:
                on_progress("analyzing keys", done, len(missing))
    finally:
        analyzer.close()
    return dataclasses.replace(library, tracks=tracks), record


def replay_keys(library: SourceLibrary, record: dict[str, Any]) -> SourceLibrary:
    """Verify against the recorded predictions without rerunning the model.

    Called only after the manifest hash is checked against the publish journal.
    A model prediction is not a correctness oracle for musical-key accuracy.
    """
    if record.get("provider") != "OpenKeyScan" or not isinstance(record.get("tracks"), list):
        raise FatalError("Invalid OpenKeyScan analysis record")
    tracks = dict(library.tracks)
    seen: set[int] = set()
    for row in record["tracks"]:
        if not isinstance(row, dict):
            raise FatalError("Invalid OpenKeyScan track record")
        tid, key = row.get("track_id"), row.get("key")
        if not isinstance(tid, int) or tid not in tracks or tid in seen:
            raise FatalError("OpenKeyScan record has an unknown or duplicate track")
        seen.add(tid)
        track = tracks[tid]
        if (track.key_name or "").strip() or not isinstance(key, str) or not _KEY.fullmatch(key):
            raise FatalError("OpenKeyScan record cannot replace an existing key")
        if track.resolved_path is None or file_hash(track.resolved_path) != row.get("audio_sha256"):
            raise FatalError(f"Audio for OpenKeyScan track {tid} changed or is missing; reconvert")
        tracks[tid] = dataclasses.replace(track, key_name=key)
    return dataclasses.replace(library, tracks=tracks)
