"""Environment and schema-support diagnostics (strictly read-only).

Answers \"will rb2engine work here, and if not, why\" before convert. Reports
tool/runtime versions, bundled DDL versions, optional Engine ``m.db`` schema
support, and optional rekordbox stick layout via :func:`scan_drive`.

Never writes anything, anywhere — SQLite opens use URI ``mode=ro``.
"""

from __future__ import annotations

import importlib.metadata
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import rb2engine
from rb2engine.errors import UnsupportedFormatError
from rb2engine.reader.scan import StickLayout, scan_drive
from rb2engine.writer.schema import SUPPORTED_SCHEMAS

# Runtime packages whose versions matter for support / bug reports.
_RUNTIME_DEPS: tuple[str, ...] = ("construct", "pyrekordbox", "click", "rich")

# Capture runbook for unsupported schema triples (relative to repo / install docs).
_DDL_README = "src/rb2engine/writer/ddl/README.md"


@dataclass(frozen=True, slots=True)
class DoctorResult:
    """Read-only diagnosis snapshot.

    ``ok`` is True when every check that was attempted passed. False when the
    user must act before convert (unsupported schema, unreadable target, or
    drive layout that convert would reject).
    """

    ok: bool
    rb2engine_version: str
    python_version: str
    bundled_schemas: tuple[tuple[int, int, int], ...]
    dependencies: tuple[tuple[str, str], ...]
    """(package_name, version_or_missing) in report order."""
    lines: tuple[str, ...]
    """Pre-rendered report lines (without trailing newline on the whole text)."""

    def render_text(self) -> str:
        """Human-readable multi-line report for stdout."""
        return "\n".join(self.lines) + "\n"


def doctor_report(
    engine_db: Path | None = None,
    drive_root: Path | None = None,
) -> DoctorResult:
    """Collect environment, schema, and optional drive diagnostics.

    Parameters
    ----------
    engine_db:
        Existing Engine ``m.db`` to inspect for Information schema triple/uuid
        and SUPPORTED_SCHEMAS membership. Opened read-only.
    drive_root:
        Rekordbox USB / folder root. Runs :func:`scan_drive` and reports
        export.pdb / exportExt.pdb (G1d), USBANLZ counts, Contents/, Engine
        Library full-rebuild path, and any existing ``m.db`` schema/uuid.

    Returns
    -------
    DoctorResult
        Frozen result with ``ok`` and :meth:`DoctorResult.render_text`.
    """
    lines: list[str] = []
    issues: list[str] = []

    py_ver = (
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )
    bundled = tuple(sorted(SUPPORTED_SCHEMAS.keys()))
    deps = _dependency_versions()

    lines.append("rb2engine doctor")
    lines.append("----------------")
    lines.append(f"rb2engine version:  {rb2engine.__version__}")
    lines.append(f"Python version:     {py_ver}")
    lines.append("")
    lines.append("Bundled Engine DDL schemas:")
    if bundled:
        for maj, minor, patch in bundled:
            lines.append(f"  {maj}.{minor}.{patch}")
    else:
        lines.append("  (none registered in SUPPORTED_SCHEMAS)")
        issues.append("No bundled DDL schemas registered.")
    lines.append("")
    lines.append("Runtime dependencies:")
    for name, ver in deps:
        lines.append(f"  {name}: {ver}")

    if engine_db is not None:
        lines.append("")
        _report_engine_db(Path(engine_db), lines, issues)

    if drive_root is not None:
        lines.append("")
        _report_drive(Path(drive_root), lines, issues)

    lines.append("")
    if issues:
        lines.append("Status: NOT OK — fix the issues above before convert.")
        for msg in issues:
            lines.append(f"  - {msg}")
        ok = False
    else:
        lines.append("Status: OK — no blocking issues detected.")
        ok = True

    return DoctorResult(
        ok=ok,
        rb2engine_version=rb2engine.__version__,
        python_version=py_ver,
        bundled_schemas=bundled,
        dependencies=deps,
        lines=tuple(lines),
    )


def _dependency_versions() -> tuple[tuple[str, str], ...]:
    out: list[tuple[str, str]] = []
    for name in _RUNTIME_DEPS:
        try:
            ver = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            ver = "not installed"
        out.append((name, ver))
    return tuple(out)


def _report_engine_db(path: Path, lines: list[str], issues: list[str]) -> None:
    lines.append(f"Engine database: {path}")
    info = _read_information(path)
    if info is None:
        msg = (
            f"Could not read Information from {path} "
            "(missing, unreadable, or not an Engine m.db)."
        )
        lines.append(f"  {msg}")
        issues.append(msg)
        return

    triple, db_uuid = info
    maj, minor, patch = triple
    triple_s = f"{maj}.{minor}.{patch}"
    lines.append(f"  schema version:   {triple_s}")
    lines.append(f"  uuid:             {db_uuid}")

    if triple in SUPPORTED_SCHEMAS:
        lines.append(f"  support:          supported ({triple_s} is bundled)")
    else:
        supported = ", ".join(
            f"{a}.{b}.{c}" for a, b, c in sorted(SUPPORTED_SCHEMAS)
        )
        msg = (
            f"Unsupported Engine schema version {triple_s}. "
            f"Supported versions: {supported or '(none)'}. "
            f"To capture a new DDL from Engine's own m.db, see {_DDL_README}."
        )
        lines.append("  support:          UNSUPPORTED")
        lines.append(f"  fix:              {msg}")
        issues.append(msg)


def _report_drive(root: Path, lines: list[str], issues: list[str]) -> None:
    lines.append(f"Drive root: {root}")
    try:
        layout = scan_drive(root)
    except UnsupportedFormatError as exc:
        lines.append(f"  scan:             FAILED — {exc}")
        issues.append(str(exc))
        return
    except OSError as exc:
        msg = f"Could not scan drive root {root}: {exc}"
        lines.append(f"  scan:             FAILED — {exc}")
        issues.append(msg)
        return

    _append_layout(layout, lines)

    if layout.engine_library_dir is not None:
        m_db = _find_m_db(layout.engine_library_dir)
        if m_db is None:
            lines.append("  existing m.db:    not found under Engine Library/Database2/")
        else:
            lines.append(f"  existing m.db:    {m_db}")
            info = _read_information(m_db)
            if info is None:
                lines.append(
                    "  existing schema:  unreadable (not a valid Engine Information row)"
                )
            else:
                triple, db_uuid = info
                maj, minor, patch = triple
                triple_s = f"{maj}.{minor}.{patch}"
                lines.append(f"  existing schema:  {triple_s}")
                lines.append(f"  existing uuid:    {db_uuid}")
                if triple in SUPPORTED_SCHEMAS:
                    lines.append(
                        f"  existing support: supported ({triple_s} is bundled)"
                    )
                else:
                    supported = ", ".join(
                        f"{a}.{b}.{c}" for a, b, c in sorted(SUPPORTED_SCHEMAS)
                    )
                    msg = (
                        f"Existing Engine Library m.db has unsupported schema "
                        f"{triple_s}. Supported versions: {supported or '(none)'}. "
                        f"To capture a new DDL from Engine's own m.db, see "
                        f"{_DDL_README}."
                    )
                    lines.append("  existing support: UNSUPPORTED")
                    lines.append(f"  fix:              {msg}")
                    issues.append(msg)


def _append_layout(layout: StickLayout, lines: list[str]) -> None:
    lines.append(f"  export.pdb:       present ({layout.export_pdb})")
    if layout.export_ext_pdb is not None:
        lines.append(
            f"  exportExt.pdb:    present (G1d MyTags; not converted) "
            f"({layout.export_ext_pdb})"
        )
    else:
        lines.append("  exportExt.pdb:    absent")

    counts = _count_usbanlz(layout.usbanlz_dir)
    lines.append(f"  USBANLZ/:         {layout.usbanlz_dir}")
    lines.append(
        f"  USBANLZ counts:   "
        f".DAT={counts['.DAT']}, .EXT={counts['.EXT']}, .2EX={counts['.2EX']}"
    )

    if layout.contents_dir is not None:
        lines.append(f"  Contents/:        present ({layout.contents_dir})")
    else:
        lines.append("  Contents/:        absent")

    if layout.engine_library_dir is not None:
        lines.append(
            f"  Engine Library/:  present ({layout.engine_library_dir}) "
            "→ full-rebuild path"
        )
    else:
        lines.append("  Engine Library/:  absent (first-run create path)")


def _count_usbanlz(usbanlz_dir: Path) -> dict[str, int]:
    """Count ANLZ siblings under USBANLZ (case-insensitive extensions)."""
    counts = {".DAT": 0, ".EXT": 0, ".2EX": 0}
    try:
        for path in usbanlz_dir.rglob("*"):
            if not path.is_file():
                continue
            suf = path.suffix.upper()
            if suf in counts:
                counts[suf] += 1
    except OSError:
        pass
    return counts


def _find_m_db(engine_library_dir: Path) -> Path | None:
    """Locate Database2/m.db under Engine Library (case-insensitive names)."""
    db2 = _find_child_dir(engine_library_dir, "Database2")
    if db2 is None:
        return None
    candidate = _find_child_file(db2, "m.db")
    return candidate


def _find_child_dir(parent: Path, name: str) -> Path | None:
    if not parent.is_dir():
        return None
    target = name.lower()
    try:
        for entry in parent.iterdir():
            if entry.name.lower() == target and entry.is_dir():
                return entry
    except OSError:
        return None
    return None


def _find_child_file(parent: Path, name: str) -> Path | None:
    if not parent.is_dir():
        return None
    target = name.lower()
    try:
        for entry in parent.iterdir():
            if entry.name.lower() == target and entry.is_file():
                return entry
    except OSError:
        return None
    return None


def _read_information(
    m_db_path: Path,
) -> tuple[tuple[int, int, int], str] | None:
    """Read schema triple + uuid from Information. Read-only; None on failure."""
    path = Path(m_db_path)
    if not path.is_file():
        return None
    try:
        # URI mode=ro: no journal, no side files, no writes.
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            """
            SELECT schemaVersionMajor, schemaVersionMinor, schemaVersionPatch, uuid
            FROM Information
            LIMIT 1
            """
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()

    if row is None:
        return None
    major, minor, patch, db_uuid = row
    if not all(isinstance(v, int) for v in (major, minor, patch)):
        return None
    if db_uuid is None:
        db_uuid = ""
    return (int(major), int(minor), int(patch)), str(db_uuid)
