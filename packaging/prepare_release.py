"""Require tested desktop and Python assets before publishing a complete release."""
from __future__ import annotations

import hashlib
import json
import sys
import tomllib
import zipfile
from pathlib import Path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(folder: Path, version: str) -> None:
    expected = {f"rb2engine-{version}-py3-none-any.whl", f"rb2engine-{version}.tar.gz"}
    for label in ("windows-x64", "macos-arm64"):
        name = f"rb2engine-{version}-{label}"
        expected.update(name + extension for extension in (".zip", ".sha256", ".json"))
        archive = folder / f"{name}.zip"
        metadata = json.loads((folder / f"{name}.json").read_text(encoding="utf-8"))
        actual = digest(archive)
        assert metadata["version"] == version and metadata["platform"] == label
        assert metadata["sha256"] == actual, f"Desktop metadata mismatch: {label}"
        assert (folder / f"{name}.sha256").read_text(encoding="utf-8").split() == [
            actual, archive.name
        ], f"Desktop checksum mismatch: {label}"
        with zipfile.ZipFile(archive) as zipped:
            assert zipped.testzip() is None, f"Corrupt desktop ZIP: {label}"
            names = zipped.namelist()
            source = next(n for n in names if n.endswith("licenses/FFmpeg/ffmpeg-8.0.3.tar.xz"))
            assert hashlib.sha256(zipped.read(source)).hexdigest() == (
                "6136812ea6d4e68bdba27e33c2a94382711cdf4f8602ffef056ff792bd6f9818"
            ), f"Corresponding FFmpeg source mismatch: {label}"
            suffix = ".exe" if label == "windows-x64" else ""
            assert any(n.endswith("native_runtime/aac-reader" + suffix) for n in names)
            suffix = ".dll" if label == "windows-x64" else ".dylib"
            assert any(n.endswith("native_runtime/overview-dsp" + suffix) for n in names)
    actual_names = {p.name for p in folder.iterdir() if p.is_file()} - {"SHA256SUMS"}
    assert actual_names == expected, f"Unexpected release asset set: {actual_names ^ expected}"
    sums = "".join(f"{digest(folder / name)}  {name}\n" for name in sorted(expected))
    (folder / "SHA256SUMS").write_text(sums, encoding="utf-8")
    print(f"Verified {len(expected)} assets for v{version}")


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    version = project["project"]["version"]
    prepare(Path(sys.argv[1]), version)
