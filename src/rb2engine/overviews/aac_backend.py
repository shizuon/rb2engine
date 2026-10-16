"""Compile a small open-source FFmpeg API client; no Engine libraries used."""

import platform
import shlex
import shutil
import subprocess
from pathlib import Path

from .formats import digest

HERE = Path(__file__).resolve().parent
LIBRARIES = ("libavformat", "libavcodec", "libavutil")


def build():
    from .runtime import binary, libraries

    bundled = binary("aac-reader")
    if bundled is not None:
        return bundled, {"sha256": digest(bundled), "libraries": libraries(),
                         "platform": platform.system(), "architecture": platform.machine()}
    from ._build import supported_platform

    supported_platform()
    pkg = shutil.which("pkg-config")
    if not pkg and Path("/opt/homebrew/bin/pkg-config").is_file():
        pkg = "/opt/homebrew/bin/pkg-config"
    if not pkg:
        raise RuntimeError("AAC needs pkg-config and FFmpeg development headers/libraries")

    def query(*args):
        result = subprocess.run(  # noqa: S603 - argument vector; no shell
            [pkg, *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(
                "AAC needs FFmpeg development headers/libraries: " + result.stderr[-2000:]
            )
        return result.stdout.strip()

    flags = shlex.split(query("--cflags", "--libs", *LIBRARIES))
    library_identity = {}
    for name in LIBRARIES:
        folder = Path(query("--variable=libdir", name))
        choices = [folder / (name + ".dylib"), folder / (name + ".so")]
        binary = next((p.resolve() for p in choices if p.is_file()), None)
        if binary is None:
            raise RuntimeError(f"Cannot identify dynamic library {name} in {folder}")
        library_identity[name] = {
            "version": query("--modversion", name),
            "path": str(binary),
            "sha256": digest(binary),
        }
    from ._build import cached_build

    source = HERE / "aac_decode.c"
    identity = {
        "source_sha256": digest(source),
        "libraries": library_identity,
        "platform": platform.system(),
        "architecture": platform.machine(),
        "flags": flags,
    }
    target, _ = cached_build(
        source,
        "aac-reader",
        shared=False,
        flags=["-O2"],
        dependencies={"libraries": library_identity, "link_flags": flags},
    )
    return target, {"sha256": digest(target), **identity}
