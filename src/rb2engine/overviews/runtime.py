"""Locate the replaceable native runtime supplied with desktop distributions."""
from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path


def directory() -> Path | None:
    root = Path(__file__).resolve().parents[1] / "native_runtime"
    if not getattr(sys, "frozen", False):
        override = os.environ.get("RB2ENGINE_NATIVE_RUNTIME")
        if override:
            root = Path(override).expanduser().resolve(strict=True)
    if root.is_dir():
        return root
    if getattr(sys, "frozen", False):
        raise RuntimeError("The desktop waveform runtime is missing. Reinstall the application.")
    return None


def binary(name: str) -> Path | None:
    root = directory()
    if root is None:
        return None
    windows = platform.system() == "Windows"
    if name == "overview-dsp":
        suffix = ".dll" if windows else ".dylib"
    else:
        suffix = ".exe" if windows else ""
    path = root / (name + suffix)
    if not path.is_file():
        raise RuntimeError(f"Desktop waveform component is missing: {path.name}")
    return path


def libraries() -> dict:
    from .formats import digest

    root = directory()
    if root is None:
        return {}
    return {p.name: {"path": str(p), "sha256": digest(p)}
            for p in sorted(root.iterdir()) if p.suffix in (".dll", ".dylib")}


def subprocess_options() -> dict:
    # Bundled native tools load their DLLs beside their executable. Hide console
    # windows and decode ffprobe's JSON as UTF-8 even on non-UTF-8 Windows hosts.
    return {"encoding": "utf-8", "errors": "replace", **(
        {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
        if sys.platform == "win32" else {}
    )}
