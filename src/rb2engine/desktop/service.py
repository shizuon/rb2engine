"""Thread-safe desktop operations using the same reader/writer as the CLI."""
from __future__ import annotations

import copy
import logging
import os
import string
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rb2engine import __version__
from rb2engine.errors import FatalError
from rb2engine.reader.library import read_library
from rb2engine.report import REPORT_FILENAME, ConversionReport, append_journal
from rb2engine.writer.build import build_library

logger = logging.getLogger(__name__)


def discover_drives() -> list[dict[str, str]]:
    """Enumerate local volumes, including USB SSDs Windows calls fixed disks."""
    roots: list[Path] = []
    if sys.platform == "win32":
        import ctypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
        kernel.GetDriveTypeW.restype = ctypes.c_uint
        # Skip network/CD/floppy probing: disconnected shares can block the UI.
        roots = [Path(f"{letter}:\\") for letter in string.ascii_uppercase[2:]
                 if kernel.GetDriveTypeW(f"{letter}:\\") in (2, 3)]
    elif sys.platform == "darwin":
        roots = list(Path("/Volumes").iterdir())
    else:
        for base in (Path("/media") / os.environ.get("USER", ""), Path("/mnt")):
            if base.is_dir():
                roots.extend(base.iterdir())
    found = []
    for root in roots:
        try:
            if root.is_dir():
                found.append({"path": str(root), "name": root.name or str(root)})
        except OSError:
            continue
    return found


class DesktopService:
    """One operation at a time, with snapshots polled by the native webview."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._inspection: dict[str, Any] | None = None
        self._window: Any = None
        self._analyzer_path = ""
        self._state: dict[str, Any] = {
            "version": __version__, "busy": False, "status": "idle", "operation": "",
            "phase": "", "done": 0, "total": 0, "path": "", "summary": None,
            "error": "", "details": "", "result": None, "close_blocked": False,
        }

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._state)

    def get_drives(self) -> list[dict[str, str]]:
        return discover_drives()

    def choose_folder(self) -> str | None:
        import webview

        with self._lock:
            if self._state["busy"] or self._window is None:
                return None
        chosen = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        return str(chosen[0]) if chosen else None

    def inspect_drive(self, path: str) -> dict[str, Any]:
        return self._start("inspect", lambda: self._inspect(path))

    def get_keyscan(self) -> dict[str, str]:
        from rb2engine.keyscan import find_installed_analyzer, resolve_analyzer, saved_analyzer

        if self._analyzer_path:
            try:
                self._analyzer_path = str(resolve_analyzer(self._analyzer_path))
            except FatalError:
                self._analyzer_path = ""
        if not self._analyzer_path:
            self._analyzer_path = saved_analyzer() or find_installed_analyzer()
        return {"path": self._analyzer_path}

    def choose_keyscan(self) -> dict[str, str]:
        import webview

        from rb2engine.keyscan import resolve_analyzer, save_analyzer

        if self._window is not None and not self.get_state()["busy"]:
            chosen = self._window.create_file_dialog(webview.FileDialog.OPEN)
            if chosen:
                self._analyzer_path = str(resolve_analyzer(str(chosen[0])))
                save_analyzer(self._analyzer_path)
        return self.get_keyscan()

    def open_keyscan_download(self) -> None:
        import webbrowser

        from rb2engine.keyscan import DOWNLOAD_URL

        webbrowser.open(DOWNLOAD_URL)

    def convert_drive(
        self, artwork: bool, confirmed: bool, replace: bool, fill_keys: bool = False,
        fill_waveforms: bool = False,
        repair_grid: bool = False, grid_mode: str = "strict",
    ) -> dict[str, Any]:
        with self._lock:
            if self._inspection is None:
                return {"ok": False, "error": "inspect_first"}
            if not confirmed:
                return {"ok": False, "error": "close_apps"}
            if self._inspection["existing"] and not replace:
                return {"ok": False, "error": "confirm_replace"}
            # Capture the inspected root, never accept a second arbitrary write target.
            checked = dict(self._inspection)
            return self._start(
                "convert",
                lambda: self._convert(checked, artwork, replace, fill_keys, fill_waveforms,
                                      repair_grid, grid_mode),
            )

    def fill_waveforms(self, confirmed: bool) -> dict[str, Any]:
        with self._lock:
            if self._inspection is None or not self._inspection["existing"]:
                return {"ok": False, "error": "inspect_first"}
            if not confirmed:
                return {"ok": False, "error": "close_apps"}
            checked = dict(self._inspection)
            return self._start("waveforms", lambda: self._waveforms(checked))

    def _waveforms(self, checked: dict[str, Any]) -> None:
        from rb2engine.desktop.waveforms import describe, fill

        result = fill(Path(checked["path"]) / "Engine Library/Database2/m.db", self._progress)
        self._update(status="warning" if result["failures"] else "waveforms_complete",
                     details=describe(result))
        with self._lock:
            if self._state["result"] is None:
                self._state["result"] = {}
            self._state["result"]["waveforms"] = result

    def verify_drive(self) -> dict[str, Any]:
        with self._lock:
            if self._inspection is None:
                return {"ok": False, "error": "inspect_first"}
            checked = dict(self._inspection)
            return self._start("verify", lambda: self._verify(checked))

    def _can_close(self) -> bool:
        with self._lock:
            if self._state["busy"]:
                self._state["close_blocked"] = True
                return False
        return True

    def _update(self, **values: Any) -> None:
        with self._lock:
            self._state.update(values)

    def _progress(self, phase: str, done: int, total: int) -> None:
        self._update(phase=phase, done=done, total=total)

    def _start(self, operation: str, work: Callable[[], None]) -> dict[str, Any]:
        with self._lock:
            if self._state["busy"]:
                return {"ok": False, "error": "busy"}
            self._state.update(busy=True, operation=operation, status="working", error="",
                               phase="scanning", done=0, total=0, close_blocked=False)
            self._thread = threading.Thread(target=self._run, args=(work,), daemon=False)
            self._thread.start()
        return {"ok": True}

    def _run(self, work: Callable[[], None]) -> None:
        try:
            work()
        except Exception as exc:
            logger.exception("Desktop operation failed")
            self._update(status="error", error=str(exc))
        finally:
            self._update(busy=False, close_blocked=False)

    def _inspect(self, path: str) -> None:
        with self._lock:
            self._inspection = None
        self._update(summary=None, result=None, details="")
        if not path or not path.strip():
            raise FatalError("Choose the root folder of a rekordbox USB export.")
        root = Path(path).expanduser().resolve(strict=True)
        self._update(path=str(root))
        library = read_library(root, with_anlz=False, with_artwork=False,
                               on_progress=self._progress)
        if library.fingerprint is None:
            raise FatalError("The source export could not be fingerprinted.")
        summary = {
            "tracks": len(library.tracks),
            "playlists": sum(not p.is_folder for p in library.playlists),
            "missing": sum(t.resolved_path is None for t in library.tracks.values()),
            "existing": (root / "Engine Library" / "Database2" / "m.db").exists(),
        }
        with self._lock:
            self._inspection = {**summary, "path": str(root), "sha256": library.fingerprint.sha256}
        self._update(summary=summary, status="ready", details="\n".join(library.warnings))

    def _convert(
        self, checked: dict[str, Any], artwork: bool, replace: bool, fill_keys: bool,
        fill_waveforms: bool = False,
        repair_grid: bool = False, grid_mode: str = "strict",
    ) -> None:
        root = Path(checked["path"])
        engine = root / "Engine Library"
        if (engine / "Database2" / "m.db").exists() and not replace:
            raise FatalError("An Engine database appeared after inspection. Inspect again.")
        library = read_library(root, with_anlz=True, with_artwork=artwork,
                               on_progress=self._progress)
        if library.fingerprint is None or library.fingerprint.sha256 != checked["sha256"]:
            with self._lock:
                self._inspection = None
            self._update(summary=None)
            raise FatalError("The rekordbox export changed. Inspect it again before converting.")
        report = ConversionReport()
        waveform_context = None
        if fill_waveforms:
            from rb2engine.overviews.generator import create_context

            self._progress("preparing waveforms", 0, 0)
            waveform_context = create_context()
        if fill_keys and any(not (t.key_name or "").strip() for t in library.tracks.values()):
            from rb2engine.keyscan import file_hash, fill_missing_keys, resolve_analyzer
            from rb2engine.reader.scan import scan_drive

            executable = resolve_analyzer(self.get_keyscan()["path"])
            library, report.key_analysis = fill_missing_keys(
                library, executable, on_progress=self._progress,
            )
            if file_hash(scan_drive(root).export_pdb) != checked["sha256"]:
                with self._lock:
                    self._inspection = None
                self._update(summary=None)
                raise FatalError("The export changed during key analysis. Inspect it again.")
        if repair_grid:
            from rb2engine.gridrepair.repair import repair_library, validate_inputs

            library, report.grid_repair = repair_library(
                library, mode=grid_mode, on_progress=self._progress,
            )
            validate_inputs(library, report.grid_repair)
        self._progress("building library", 0, 0)
        database = build_library(library, drive_root=root, report=report,
                                 with_artwork=artwork, on_progress=self._progress,
                                 backup_existing=True, allow_existing=replace,
                                 target_schema=(3, 0, 2) if fill_waveforms else None)
        notices = list(library.warnings)
        analysis_text = ""
        if report.grid_repair is not None:
            grid = report.grid_repair
            if grid["failed"]:
                notices.append("Some grid checks failed; their original grids were retained.")
        if report.key_analysis is not None:
            analysis = report.key_analysis
            analysis_text += f"\nOpenKeyScan: {len(analysis['tracks'])} missing keys filled.\n"
            notices.extend(f"OpenKeyScan track {row['track_id']}: {row['error']}"
                           for row in analysis["failures"])
        if report.provenance is not None:
            try:
                append_journal(engine, report.provenance)
            except OSError as exc:
                notices.append(f"Database saved, but provenance journal could not be saved: {exc}")
        report_path: str | None = None
        try:
            report_path = str(report.write_json(engine / REPORT_FILENAME))
        except OSError as exc:
            notices.append(f"Database saved, but report could not be saved: {exc}")
        counters = report.to_json_obj()["counters"]
        attention = bool(notices or counters["tracks_skipped"] or counters["cues_dropped"]
                         or counters["loops_dropped"])
        with self._lock:
            if self._inspection is not None:
                self._inspection["existing"] = True
            summary = dict(self._state["summary"])
            summary["existing"] = True
        self._update(status="warning" if attention else "complete", summary=summary,
                     result={"database": str(database), "report": report_path,
                             "counters": counters, "verified": None,
                             "grid_repair": report.grid_repair},
                     details=report.render_text() + analysis_text + "\n\n" + "\n".join(notices))
        if fill_waveforms:
            from rb2engine.desktop.waveforms import describe, fill

            try:
                waveforms = fill(database, self._progress, context=waveform_context, rebuilt=True)
                with self._lock:
                    self._state["result"]["waveforms"] = waveforms
                    self._state["details"] += "\n\n" + describe(waveforms)
                if waveforms["failures"]:
                    self._update(status="warning")
            except Exception as exc:
                logger.exception("Conversion saved but waveform completion failed")
                with self._lock:
                    self._state["details"] += (
                        f"\n\nConversion saved; waveform completion failed: {exc}"
                    )
                self._update(status="warning")

    def _verify(self, checked: dict[str, Any]) -> None:
        from rb2engine.verify import verify_library

        self._progress("verifying", 0, 0)
        result = verify_library(Path(checked["path"]))
        with self._lock:
            waveform_result = (self._state.get("result") or {}).get("waveforms")
        if waveform_result and waveform_result.get("batch"):
            from rb2engine.overviews.publication import verify

            waveform_result["verified"] = verify(waveform_result["batch"])
        self._update(status="verified" if result.exit_code == 0 else "warning",
                     details=result.render_text())
        with self._lock:
            if self._state["result"] is not None:
                self._state["result"]["verified"] = result.exit_code == 0
