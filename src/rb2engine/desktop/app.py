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


def _smoke(window: Any, service: DesktopService, output: Path, drive: str | None) -> None:
    """Packaged-app smoke: real renderer, bridge, DOM buttons and converter."""
    result: dict[str, Any] = {"ok": False}
    deadline = time.monotonic() + 80
    try:
        while not window.evaluate_js("Boolean(window.rb2engineReady)"):
            if time.monotonic() > deadline:
                raise TimeoutError("The native bridge did not become ready")
            time.sleep(0.1)
        result["renderer"] = window.evaluate_js("navigator.userAgent")
        result["title"] = window.evaluate_js("document.title")
        if drive:
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
        window = webview.create_window(
            "rb2engine", html=render_html(), js_api=service,
            width=660, height=430, min_size=(520, 360), background_color="#f7f8f4",
            text_select=True,
        )
        if window is None:
            raise RuntimeError("Could not create the application window")
        service._window = window
        window.events.closing += service._can_close
        # Edge Chromium is required. Never silently fall back to obsolete MSHTML.
        options: dict[str, Any] = {"gui": "edgechromium" if sys.platform == "win32" else None}
        if args.smoke_test:
            webview.start(_smoke, (window, service, args.smoke_test, args.smoke_drive), **options)
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
