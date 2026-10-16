"""Native WebKit / WebView2 shell; no browser server or network assets."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from rb2engine.desktop.service import DesktopService


def render_html() -> str:
    assets = Path(__file__).parent / "assets"
    return (assets / "index.html").read_text(encoding="utf-8").replace(
        "/* APP_CSS */", (assets / "app.css").read_text(encoding="utf-8")
    ).replace("/* APP_JS */", (assets / "app.js").read_text(encoding="utf-8"))


def _smoke(
    window: Any, service: DesktopService, output: Path, drive: str | None, analyze: bool,
    waveforms: bool = False, grid_mode: str | None = None,
) -> None:
    """Packaged-app smoke: real renderer, bridge, DOM buttons and converter."""
    result: dict[str, Any] = {"ok": False}
    deadline = time.monotonic() + (360 if grid_mode else 210 if analyze or waveforms else 80)
    try:
        while not window.evaluate_js("Boolean(window.rb2engineReady)"):
            if time.monotonic() > deadline:
                raise TimeoutError("The native bridge did not become ready")
            time.sleep(0.1)
        result["renderer"] = window.evaluate_js("navigator.userAgent")
        result["title"] = window.evaluate_js("document.title")
        assert not window.evaluate_js("document.querySelector('#repair-grid').checked")
        assert window.evaluate_js("document.querySelector('#grid-options').hidden")
        assert window.evaluate_js("document.querySelector('#grid-mode').value") == "strict"
        if grid_mode:
            window.evaluate_js(
                "document.querySelector('#repair-grid').click();"
                f"document.querySelector('#grid-mode').value={json.dumps(grid_mode)};"
            )
            assert not window.evaluate_js("document.querySelector('#grid-options').hidden")
        assert window.evaluate_js("document.querySelector('#keyscan-help').hidden")
        installed = bool(service.get_keyscan()["path"])
        window.evaluate_js("document.querySelector('#fill-keys').click();")
        prompt_query = "document.querySelector('#keyscan-help').hidden"
        while bool(window.evaluate_js(prompt_query)) != installed:
            if time.monotonic() > deadline:
                raise TimeoutError("Incorrect OpenKeyScan installation prompt visibility")
            time.sleep(0.1)
        result["keyscan"] = {"installed": installed, "download_prompt_shown": not installed}
        if not analyze:
            window.evaluate_js("document.querySelector('#fill-keys').click();")
        if waveforms:
            window.evaluate_js("document.querySelector('#fill-waveforms').click();")
        if drive:
            from rb2engine.overviews.formats import digest

            source_files = [p for folder in (Path(drive) / "PIONEER", Path(drive) / "Contents")
                            for p in folder.rglob("*") if p.is_file()]
            source_hashes = {str(p): digest(p) for p in source_files}
            window.evaluate_js(
                f"document.querySelector('#path').value={json.dumps(drive)};"
                "document.querySelector('#path').dispatchEvent(new Event('input'));"
                "document.querySelector('#inspect').click();"
            )
            _wait_operation(service, "inspect", deadline)
            if service.get_state()["status"] != "ready":
                raise RuntimeError(service.get_state()["error"])
            window.evaluate_js(
                "document.querySelector('#convert').click();"
                "document.querySelector('#confirm-convert').click();"
            )
            _wait_operation(service, "convert", deadline)
            if service.get_state()["status"] not in ("complete", "warning"):
                raise RuntimeError(service.get_state()["error"])
            window.evaluate_js("document.querySelector('#verify').click();")
            _wait_operation(service, "verify", deadline)
            if service.get_state()["status"] != "verified":
                raise RuntimeError(service.get_state()["details"])
            result["conversion"] = service.get_state()["result"]
            if grid_mode:
                grid = result["conversion"]["grid_repair"]
                assert grid["mode"] == grid_mode and grid["runtime"] == "onnxruntime"
                assert grid["corrected"] == 1 and grid["failed"] == 0, grid
                row = grid["tracks"][0]
                assert -100 <= row["time_shift_ms"] <= -50, row
                assert row["bar_phase_shift"] == 2, row
                result["grid_repair"] = grid
            if waveforms:
                import subprocess
                import tempfile

                from rb2engine.overviews import publication
                from rb2engine.overviews.decoder import executable
                from rb2engine.overviews.generator import generate
                from rb2engine.overviews.runtime import subprocess_options

                waveform_result = result["conversion"]["waveforms"]
                if waveform_result["generated"] != 1 or waveform_result["failures"]:
                    raise RuntimeError(f"Waveform generation failed: {waveform_result}")
                result["waveforms"] = waveform_result
                result["waveform_restore"] = publication.restore(waveform_result["batch"])
                # Exercise the separate button on the existing converted library.
                window.evaluate_js(
                    "document.querySelector('#waveforms').click();"
                    "document.querySelector('#confirm-convert').click();"
                )
                _wait_operation(service, "waveforms", deadline)
                final = service.get_state()
                if final["status"] != "waveforms_complete":
                    raise RuntimeError(f"Standalone waveform completion failed: {final}")
                result["standalone_waveforms"] = final["result"]["waveforms"]
                # Exercise the AAC helper from the frozen bundle as well as
                # generic decoding. Its output paths must support Unicode.
                with tempfile.TemporaryDirectory(prefix="rb2engine-aac-") as scratch:
                    audio = Path(scratch) / "音频 #1 100%.m4a"
                    subprocess.run(  # noqa: S603 - bundled executable, authored smoke input
                        [executable("ffmpeg"), "-v", "error", "-i",
                         str(Path(drive) / "Contents/test.wav"), "-c:a", "aac", str(audio)],
                        capture_output=True, check=True, **subprocess_options(),
                    )
                    generated = generate(audio, Path(scratch) / "中文缓存")
                    result["aac_runtime"] = {
                        key: generated[key] for key in
                        ("codec", "sample_rate", "declared_frames", "audio_written")
                    }
            if source_hashes != {str(p): digest(p) for p in source_files}:
                raise RuntimeError("Source export or audio changed")
            result["source_hashes_unchanged"] = True
            if analyze:
                report_path = Path(result["conversion"]["report"])
                report = json.loads(report_path.read_text(encoding="utf-8"))
                analysis = report["key_analysis"]
                if not analysis or not analysis["tracks"] or analysis["failures"]:
                    raise RuntimeError(f"OpenKeyScan did not fill the test key: {analysis}")
                result["keyscan"]["analysis"] = analysis
        result["ok"] = True
    except Exception as exc:
        result["error"] = str(exc)
        logging.exception("Desktop smoke failed")
    finally:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        window.destroy()


def _wait_operation(service: DesktopService, operation: str, deadline: float) -> None:
    while True:
        state = service.get_state()
        if state["operation"] == operation and not state["busy"]:
            # Allow the polling UI to render and enable its next button.
            time.sleep(0.6)
            return
        if time.monotonic() > deadline:
            raise TimeoutError(f"Timed out waiting for {operation}")
        time.sleep(0.1)


def main() -> None:
    parser = argparse.ArgumentParser(description="rb2engine desktop")
    parser.add_argument("--smoke-test", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--smoke-drive", help=argparse.SUPPRESS)
    parser.add_argument("--smoke-analyzer", help=argparse.SUPPRESS)
    parser.add_argument("--smoke-waveforms", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--smoke-grid", choices=("strict", "majority"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if sys.platform == "win32":
        log_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "rb2engine"
    else:
        log_dir = Path.home() / "Library" / "Logs" / "rb2engine"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(filename=log_dir / "desktop.log", encoding="utf-8", level=logging.INFO)
    except OSError:
        logging.basicConfig(level=logging.INFO)
    try:
        import webview

        service = DesktopService()
        if args.smoke_test and args.smoke_analyzer:
            from rb2engine.keyscan import resolve_analyzer

            service._analyzer_path = str(resolve_analyzer(args.smoke_analyzer))
        window = webview.create_window(
            "rb2engine", html=render_html(), js_api=service,
            width=660, height=530, min_size=(520, 450), background_color="#f7f8f4",
            text_select=True,
        )
        if window is None:
            raise RuntimeError("Could not create the application window")
        service._window = window
        window.events.closing += service._can_close
        # Edge Chromium is required. Never silently fall back to obsolete MSHTML.
        options: dict[str, Any] = {"gui": "edgechromium" if sys.platform == "win32" else None}
        if args.smoke_test:
            webview.start(
                _smoke, (window, service, args.smoke_test, args.smoke_drive,
                         bool(args.smoke_analyzer), args.smoke_waveforms, args.smoke_grid),
                **options,
            )
            if not json.loads(args.smoke_test.read_text(encoding="utf-8"))["ok"]:
                raise SystemExit(1)
        else:
            webview.start(**options)
    except Exception as exc:
        logging.exception("Cannot start rb2engine")
        message = (
            f"rb2engine could not start: {exc}\n\n"
            "Windows requires Microsoft Edge WebView2 Runtime (Evergreen).\n"
            "https://developer.microsoft.com/microsoft-edge/webview2/\n\n"
            f"Log: {log_dir / 'desktop.log'}"
        )
        if args.smoke_test:
            args.smoke_test.write_text(
                json.dumps({"ok": False, "error": message}), encoding="utf-8"
            )
        elif sys.platform == "win32":
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, "rb2engine", 0x10)
        elif sys.stderr is not None:
            print(message, file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
