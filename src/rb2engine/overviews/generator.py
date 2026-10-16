"""Content-addressed generation, resumable without trusting incomplete outputs."""

import hashlib
import json
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from .decoder import decode, executable, probe
from .dsp import build, features, reduce_points
from .formats import digest, pack, unpack
from .runtime import libraries, subprocess_options

HERE = Path(__file__).resolve().parent


def implementation_digest():
    h = hashlib.sha256()
    for path in sorted(HERE.iterdir()):
        if path.suffix in (".py", ".c"):
            h.update(path.name.encode())
            h.update(path.read_bytes())
    return h.hexdigest()


def cache_key(audio_sha, runtime, implementation):
    return hashlib.sha256(
        (audio_sha + "\n" + json.dumps(runtime, sort_keys=True) + "\n" + implementation).encode()
    ).hexdigest()


def verify_entry(folder, expected_key=None):
    folder = Path(folder)
    report = json.loads((folder / "result.json").read_text(encoding="utf-8"))
    if (
        report.get("schema") != 2
        or report["declared_frames"] <= 0
        or report["sample_rate"] not in (44100, 48000)
    ):
        raise ValueError("Unknown/invalid cache report; generate a new entry")
    identity = cache_key(
        report["source_sha256"], report["runtime"], report["implementation_sha256"]
    )
    if report["cache_key"] != identity:
        raise ValueError("Cache report identity mismatch")
    if expected_key and report["cache_key"] != expected_key:
        raise ValueError("Cache identity mismatch")
    for kind in ("tri", "rgb"):
        path = folder / f"overview.{kind}"
        if digest(path) != report["outputs_sha256"][kind]:
            raise ValueError("Cache waveform hash mismatch")
        points, clock, footer, _ = unpack(path.read_bytes())
        expected = (
            report["declared_frames"] / (report["block_frames"] * 1024) * report["block_frames"]
        )
        if clock != expected:
            raise ValueError("Cache clock mismatch")
        maximum = (
            points.max(0) if kind == "tri" else points[points.astype("uint16").sum(1).argmax()]
        )
        if footer != maximum.tobytes():
            raise ValueError("Cache footer mismatch")
    return report


@dataclass
class RuntimeContext:
    """One batch's verified dependencies; no audio-dependent values are reused."""

    library: Path
    runtime: dict[str, Any]
    implementation: str
    executables: dict[str, str] = field(default_factory=dict)
    dependencies: dict[Path, tuple[int, int, int, int]] = field(default_factory=dict)
    aac_reader: Path | None = None
    aac_identity: dict[str, Any] | None = None

    def remember(self, path):
        path = Path(path).resolve(strict=True)
        stat = path.stat()
        self.dependencies[path] = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)

    def validate(self):
        for path, expected in self.dependencies.items():
            stat = path.stat()
            if (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns) != expected:
                raise RuntimeError(
                    "Overview dependency changed during batch; create a new runtime context: "
                    + str(path)
                )

    def ensure_aac(self):
        if self.aac_reader is None:
            from .aac_backend import build as build_aac

            self.aac_reader, self.aac_identity = build_aac()
            self.remember(self.aac_reader)
            for library in self.aac_identity["libraries"].values():
                self.remember(library["path"])
        return self.aac_reader


def create_context(library=None):
    """Resolve expensive tool/build identity once, before processing a batch.

    AAC headers and libraries are checked lazily only when an AAC input is used.
    The context fails if a recorded dependency changes between songs.
    """
    from ._build import supported_platform

    supported_platform()
    compiled = Path(library or build()).resolve(strict=True)
    runtime = {}
    binaries = []
    executable_paths = {}
    for name in ("ffmpeg", "ffprobe"):
        path = Path(executable(name)).resolve(strict=True)
        binaries.append(path)
        executable_paths[name] = str(path)
        runtime[name] = {
            "sha256": digest(path),
            "version": subprocess.check_output(  # noqa: S603 - resolved native tool
                [str(path), "-version"], text=True, **subprocess_options()
            ).splitlines()[0],
        }
    runtime["dsp_sha256"] = digest(compiled)
    context = RuntimeContext(compiled, runtime, implementation_digest(), executable_paths)
    runtime["bundled_libraries"] = libraries()
    for dependency in runtime["bundled_libraries"].values():
        context.remember(dependency["path"])
    for path in [compiled, *binaries]:
        context.remember(path)
    return context


def generate(audio, cache_root, library=None, *, context=None):
    audio = Path(audio).resolve(strict=True)
    if not audio.is_file():
        raise ValueError("Audio must be a regular file")
    context = context or create_context(library)
    if library is not None and Path(library).resolve() != context.library:
        raise ValueError("Explicit DSP library differs from the runtime context")
    context.validate()
    cache_root = Path(cache_root).resolve()
    cache_root.mkdir(parents=True, exist_ok=True)
    source_sha = digest(audio)
    compiled = context.library
    metadata = probe(audio, ffprobe=context.executables.get("ffprobe"))
    runtime = dict(context.runtime)
    reader = None
    if metadata["streams"][0]["codec_name"] == "aac":
        reader = context.ensure_aac()
        runtime["aac_backend"] = context.aac_identity
    decoder_version = runtime["ffmpeg"]["version"]
    implementation = context.implementation
    identity = cache_key(source_sha, runtime, implementation)
    target = cache_root / identity
    if target.exists():
        result = verify_entry(target, identity)
        if result["source_sha256"] != source_sha:
            raise ValueError("Unexpected cache collision")
        return {
            **result,
            "cache_path": str(target),
            "cache_hit": True,
            "requested_source": str(audio),
        }
    with tempfile.TemporaryDirectory(prefix=".pending-", dir=cache_root) as temp:
        staging = Path(temp)
        decoded = decode(
            audio,
            staging,
            metadata=metadata,
            aac_reader=reader,
            ffmpeg=context.executables.get("ffmpeg"),
        )
        outputs = {}
        block = None
        for kind in ("tri", "rgb"):
            values, block = features(decoded, kind, compiled)
            points = reduce_points(values, block, decoded.frames)
            blob = pack(points, decoded.frames, block, kind)
            file = staging / f"overview.{kind}"
            file.write_bytes(blob)
            outputs[kind] = digest(file)
        context.validate()
        if digest(audio) != source_sha:
            raise ValueError("Audio changed during generation")
        result = {
            "schema": 2,
            "version": __version__,
            "cache_key": identity,
            "implementation_sha256": implementation,
            "decoder": decoder_version,
            "runtime": runtime,
            "source_at_generation": str(audio),
            "source_sha256": source_sha,
            "codec": decoded.plan["codec"],
            "sample_rate": decoded.rate,
            "channels": decoded.channels,
            "declared_frames": decoded.frames,
            "decoded_frames": decoded.decoded_frames,
            "processed_frames": decoded.processed_frames,
            "block_frames": block,
            "timing_plan": decoded.plan,
            "warnings": decoded.warnings,
            "outputs_sha256": outputs,
            "database_written": False,
            "audio_written": False,
            "hardware_validated": False,
        }
        del decoded
        (staging / "decoded.f32").unlink()
        (staging / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        verify_entry(staging, identity)
        try:
            staging.rename(target)
        except OSError:
            if not target.exists():
                raise
            concurrent = verify_entry(target, identity)
            if concurrent["outputs_sha256"] != outputs:
                raise ValueError("Concurrent generation produced different bytes") from None
    return {**result, "cache_path": str(target), "cache_hit": False, "requested_source": str(audio)}
