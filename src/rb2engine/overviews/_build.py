"""User-owned, verified native builds; never write into the installed package."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


def supported_platform() -> str:
    system = platform.system()
    if system not in ("Darwin", "Linux", "Windows"):
        raise RuntimeError("Overview generation supports macOS, Linux and Windows only")
    return system


def build_cache() -> Path:
    """RB2ENGINE_OVERVIEWS_BUILD_CACHE overrides the per-user native build cache."""
    system = supported_platform()
    override = os.environ.get("RB2ENGINE_OVERVIEWS_BUILD_CACHE")
    if override:
        return Path(override).expanduser().resolve()
    if system == "Darwin":
        root = Path.home() / "Library" / "Caches"
    elif system == "Windows":
        root = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
    else:
        root = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    return root / "rb2engine" / "overviews" / "build"


def sha(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1048576), b""):
            value.update(chunk)
    return value.hexdigest()


def compiler_configuration() -> tuple[str, list[str], dict[str, Any]]:
    system = supported_platform()
    if system == "Windows":
        raise RuntimeError(
            "Use the desktop waveform runtime on Windows; "
            "build it with packaging/build_native.sh"
        )
    if system == "Darwin":
        compiler = subprocess.check_output(
            ["xcrun", "--find", "clang"],  # noqa: S607 - Xcode toolchain lookup
            text=True,
        ).strip()
        sdk = subprocess.check_output(
            ["xcrun", "--show-sdk-path"],  # noqa: S607 - Xcode SDK lookup
            text=True,
        ).strip()
        flags = ["-isysroot", sdk]
    else:
        compiler = shutil.which("cc") or ""
        flags = []
        if not compiler:
            raise RuntimeError("C compiler required for overview generation")
    version = subprocess.check_output([compiler, "--version"], text=True).splitlines()[0]  # noqa: S603
    identity = {
        "compiler": str(Path(compiler).resolve()),
        "compiler_sha256": sha(Path(compiler)),
        "compiler_version": version,
        "platform": system,
        "architecture": platform.machine(),
    }
    return compiler, flags, identity


def cached_build(
    source: Path,
    name: str,
    *,
    shared: bool,
    flags: list[str],
    dependencies: dict[str, Any] | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Atomic cache population; a corrupt artifact fails rather than being executed."""
    compiler, platform_flags, compiler_identity = compiler_configuration()
    system = supported_platform()
    suffix = (".dylib" if system == "Darwin" else ".so") if shared else ""
    link_flags = []
    if shared:
        link_flags = (
            ["-dynamiclib", "-Wl,-install_name,@rpath/" + name + suffix]
            if system == "Darwin"
            else ["-shared", "-fPIC"]
        )
    # Numerical compiler flags and their order are part of the build identity.
    all_flags = [*flags, *platform_flags, *link_flags]
    identity = {
        "schema": 1,
        "source_sha256": sha(source),
        **compiler_identity,
        "flags": all_flags,
        "dependencies": dependencies or {},
    }
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    root = build_cache()
    root.mkdir(parents=True, exist_ok=True)
    destination = root / key
    artifact = destination / (name + suffix)
    manifest = destination / "build.json"
    if destination.exists():
        try:
            recorded = json.loads(manifest.read_text(encoding="utf-8"))
            if recorded["identity"] != identity or sha(artifact) != recorded["binary_sha256"]:
                raise ValueError("native artifact identity mismatch")
        except (OSError, ValueError, KeyError) as exc:
            raise RuntimeError(
                f"Invalid overview build cache {destination}; remove that entry and retry"
            ) from exc
        return artifact, recorded
    with tempfile.TemporaryDirectory(prefix=".building-", dir=root) as temporary:
        stage = Path(temporary)
        binary = stage / artifact.name
        # Keep dependent libraries after the source/object for Linux linkers.
        command = [compiler, *all_flags, str(source), "-o", str(binary)]
        if shared and system == "Linux":
            command.append("-lm")
        if dependencies:
            command.extend(dependencies.get("link_flags", []))
        result = subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603
        if result.returncode:
            raise RuntimeError("Overview native compilation failed: " + result.stderr[-4000:])
        recorded = {"identity": identity, "binary_sha256": sha(binary)}
        (stage / "build.json").write_text(json.dumps(recorded, indent=2), encoding="utf-8")
        try:
            stage.rename(destination)
        except OSError:
            if not destination.is_dir():
                raise
            # Another builder won. Validate its result rather than overwriting it.
            other = json.loads(manifest.read_text(encoding="utf-8"))
            if other["identity"] != identity or sha(artifact) != other["binary_sha256"]:
                raise RuntimeError(
                    "Concurrent overview build produced an invalid artifact"
                ) from None
            recorded = other
    return artifact, recorded
