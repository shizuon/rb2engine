"""Click app for conversion, verification and optional overview publication.

All four commands are implemented. ``inspect``, ``verify`` and ``doctor`` are
strictly read-only; ``convert`` writes only inside Engine Library/.
The optional ``waveforms`` group documents its cache and publication writes.
``inspect`` exits 0 even when the source carries warnings or skips —
inspection is not conversion.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from rb2engine import __version__
from rb2engine.errors import FatalError, UnsupportedFormatError
from rb2engine.logging import configure_logging, log_event
from rb2engine.waveforms_cli import waveforms

if TYPE_CHECKING:  # import only for typing — keeps CLI startup light
    from rb2engine.report import ConversionReport


def load_source_library(drive: Path) -> Any:
    """Parse a rekordbox USB export into a SourceLibrary.

    Reader modules are imported **lazily** so ``--help`` and the report layer
    work while pdb/anlz are still landing. Discovers a library-producing
    entrypoint on ``reader.pdb`` (or a future ``reader`` package-level
    helper). Does **not** call ``scan.scan_drive`` — that returns a layout
    descriptor, not a SourceLibrary.
    """
    drive = Path(drive)

    # Future package-level orchestration (if/when added).
    try:
        from rb2engine import reader as reader_pkg
    except ImportError:
        reader_pkg = None  # type: ignore[assignment]

    if reader_pkg is not None:
        for name in ("read_library", "load_library", "open_drive"):
            fn = getattr(reader_pkg, name, None)
            if callable(fn):
                return fn(drive)

    try:
        from rb2engine.reader import pdb as pdb_mod
    except ImportError as exc:
        raise FatalError("reader.pdb is unavailable; cannot inspect this drive yet.") from exc

    for name in ("read_library", "load_library", "parse_library", "open_pdb"):
        fn = getattr(pdb_mod, name, None)
        if callable(fn):
            return fn(drive)

    raise FatalError(
        "Source reader is not available yet "
        "(reader/pdb.py exposes no SourceLibrary load entrypoint). "
        "inspect requires a working reader, or a test double via "
        "rb2engine.cli.load_source_library."
    )


def _filter_library_track(library: Any, track_id: int) -> Any:
    """Return a SourceLibrary containing only *track_id* (same drive_root)."""
    from rb2engine.ir import SourceLibrary

    if track_id not in library.tracks:
        raise click.ClickException(f"track id {track_id} not found in source library")
    return SourceLibrary(
        drive_root=library.drive_root,
        tracks={track_id: library.tracks[track_id]},
        playlists=list(library.playlists),
        warnings=list(library.warnings),
        # getattr: test doubles for this loader predate the provenance field.
        fingerprint=getattr(library, "fingerprint", None),
    )


@click.group()
@click.version_option(version=__version__, prog_name="rb2engine")
@click.option(
    "-v",
    "--verbose",
    count=True,
    help="Increase log verbosity (-v info, -vv debug). Logs go to stderr.",
)
@click.option(
    "--log-json",
    is_flag=True,
    help="Emit one JSON log object per line on stderr (stdout stays for report/inspect).",
)
@click.pass_context
def main(ctx: click.Context, verbose: int, log_json: bool) -> None:
    """Convert rekordbox USB exports to Engine DJ libraries."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    ctx.obj["log_json"] = log_json
    configure_logging(verbose=verbose, log_json=log_json)


@main.command("inspect")
@click.argument(
    "drive",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Dump canonical IR JSON (SourceLibrary.to_json_obj) to stdout.",
)
@click.option(
    "--track",
    "track_id",
    type=int,
    default=None,
    help="Restrict dump to a single rekordbox track id.",
)
@click.pass_context
def inspect_cmd(
    ctx: click.Context,
    drive: Path,
    as_json: bool,
    track_id: int | None,
) -> None:
    """Parse the source and dump the IR without writing anything.

    Primary debugging tool and the source of golden_ir.json. Always exits 0
    on a successful parse, even when the library has warnings.
    """
    log_event("inspect", "start", detail=str(drive), level="info")
    try:
        library = load_source_library(drive)
    except (FatalError, UnsupportedFormatError) as exc:
        click.echo(str(exc), err=True)
        ctx.exit(2)
    except Exception as exc:  # noqa: BLE001 - top-level guard: any failure becomes exit 2, never a traceback
        click.echo(f"inspect failed: {exc}", err=True)
        ctx.exit(2)

    if track_id is not None:
        library = _filter_library_track(library, track_id)

    if as_json:
        # CRITICAL: use IR canonicalization — do not re-serialize paths here.
        obj = library.to_json_obj()
        sys.stdout.write(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")
    else:
        n_tracks = len(library.tracks)
        n_playlists = len(library.playlists)
        n_warnings = len(library.warnings)
        click.echo(f"drive:      {library.drive_root}")
        click.echo(f"tracks:     {n_tracks}")
        click.echo(f"playlists:  {n_playlists}")
        click.echo(f"warnings:   {n_warnings}")
        if library.warnings:
            for w in library.warnings:
                click.echo(f"  - {w}")
        for rb_id in sorted(library.tracks.keys()):
            t = library.tracks[rb_id]
            click.echo(f"  [{rb_id}] {t.artist} — {t.title}")

    log_event("inspect", "done", detail={"tracks": len(library.tracks)}, level="info")
    # Inspection is not conversion: always 0 on successful parse.
    ctx.exit(0)


def _progress_stream() -> Any:
    """The stream the progress bar draws on.

    Indirected through a function so tests can inject a stream whose
    ``isatty()`` they control; ``sys.stderr`` is resolved at call time rather
    than at import so a caller that replaces it still gets the real one.
    """
    return sys.stderr


def _emit_report(
    report: ConversionReport,
    drive: Path | None,
    override: Path | None,
) -> None:
    """Print the human report and write the machine JSON beside the library.

    Never fatal: a conversion that succeeded must not be reported as failed
    just because the report file could not be written (e.g. a full or
    read-only drive). The human summary still reaches stdout.
    """
    from rb2engine.report import resolve_report_path

    report.print_human()
    try:
        target = resolve_report_path(drive, override=override, library_ready=not report.fatal)
        written = report.write_json(target)
        click.echo(f"report: {written}")
    except OSError as exc:
        click.echo(f"warning: could not write JSON report: {exc}", err=True)
        return

    # The cwd fallback is right for fatal/early failures, but on a SUCCESSFUL
    # run it means the stick carries no record of what it was built from. This
    # really happened: a convert reported success with its drive already
    # unmounted and quietly dropped the report into the source repo. Say so.
    if (
        drive is not None
        and override is None
        and not report.fatal
        and not written.resolve().is_relative_to(Path(drive).resolve())
    ):
        click.echo(
            f"warning: the report landed at {written}, NOT on the stick — "
            "the drive's Engine Library was missing or unwritable, so no "
            "provenance record travels with this conversion. Check that the "
            "drive is still mounted and re-run convert.",
            err=True,
        )


@main.command("convert")
@click.argument(
    "drive",
    type=click.Path(exists=False, path_type=Path),
    required=False,
)
@click.option("--dry-run", is_flag=True, help="Parse and map without writing anything.")
@click.option(
    "--database-uuid",
    default=None,
    help="Override Information.uuid (default: reuse the existing one).",
)
@click.option(
    "--path-base",
    type=click.Choice(["engine-lib", "drive-root", "absolute"], case_sensitive=False),
    default=None,
    help="Track.path base (default engine-lib; absolute is diagnostic-only).",
)
@click.option("--target-schema", default=None, help="Engine schema triple, e.g. 3.0.1.")
@click.option(
    "--report",
    "report_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Override JSON report path (default: Engine Library/rb2engine-report.json).",
)
@click.option("--no-artwork", is_flag=True, help="Skip album art extraction/writes.")
@click.option("--repair-grid", is_flag=True,
              help="Experimental: repair fixed-tempo grid offsets using offline beat detection.")
@click.option("--grid-mode", type=click.Choice(["strict", "majority"]), default="strict",
              show_default=True, help="Grid repair agreement: all 3 passages or any 2.")
@click.pass_context
def convert_cmd(
    ctx: click.Context,
    drive: Path | None,
    dry_run: bool,
    database_uuid: str | None,
    path_base: str | None,
    target_schema: str | None,
    report_path: Path | None,
    no_artwork: bool,
    repair_grid: bool,
    grid_mode: str,
) -> None:
    """Convert a rekordbox USB export into an Engine Library on the same drive.

    Reads PIONEER/export.pdb + USBANLZ, then writes Engine Library/Database2/m.db.
    Music files are referenced where they already are — nothing is copied and
    nothing outside Engine Library/ is written.

    Exit codes: 0 clean, 1 converted with skips, 2 fatal (nothing usable written).
    """
    if drive is None:
        raise click.UsageError("DRIVE is required (the mount point of the stick)")

    # Imported lazily so `--help` and `--version` stay fast and do not pull in
    # the parser/writer stack.
    import rb2engine.reader.library as reader_library
    from rb2engine.progress import ProgressReporter
    from rb2engine.report import ConversionReport
    from rb2engine.writer.build import build_library

    schema: tuple[int, int, int] | None = None
    if target_schema:
        try:
            parts = tuple(int(p) for p in target_schema.split("."))
        except ValueError as exc:
            raise click.UsageError(
                f"--target-schema must look like 3.0.2, got {target_schema!r}"
            ) from exc
        if len(parts) != 3:
            raise click.UsageError(f"--target-schema must have three parts, got {target_schema!r}")
        schema = (parts[0], parts[1], parts[2])

    # A big stick spends minutes opening files; without this a running
    # conversion is indistinguishable from a hung one. Disabled under
    # --log-json, which owns stderr, and off a terminal, where a \r-redrawn
    # bar would turn a redirected log into one unreadable line.
    progress = ProgressReporter(
        _progress_stream(),
        enabled=False if ctx.obj.get("log_json") else None,
    )

    report = ConversionReport()
    try:
        library = reader_library.read_library(
            drive,
            with_anlz=True,
            with_artwork=not no_artwork,
            on_progress=progress,
        )

        if dry_run:
            progress.close()
            click.echo(
                f"dry run: {len(library.tracks)} tracks, "
                f"{len(library.playlists)} playlists — nothing written"
            )
            ctx.exit(0)

        if repair_grid:
            from rb2engine.gridrepair.repair import repair_library, validate_inputs

            library, report.grid_repair = repair_library(
                library, mode=grid_mode, on_progress=progress,
            )
            validate_inputs(library, report.grid_repair)

        m_db = build_library(
            library,
            drive_root=drive,
            report=report,
            path_base=path_base or "engine-lib",
            target_schema=schema,
            database_uuid=database_uuid,
            with_artwork=not no_artwork,
            on_progress=progress,
        )
    except UnsupportedFormatError as exc:
        progress.close()
        click.echo(f"unsupported: {exc}", err=True)
        report.fatal, report.fatal_message = True, str(exc)
        _emit_report(report, drive, report_path)
        ctx.exit(2)
    except FatalError as exc:
        progress.close()
        click.echo(f"conversion failed: {exc}", err=True)
        report.fatal, report.fatal_message = True, str(exc)
        _emit_report(report, drive, report_path)
        ctx.exit(2)
    finally:
        # Also covers ctx.exit() on the dry-run path, which raises internally.
        progress.close()

    # Provenance journal: one appended line per publish, written before the
    # success banner so a crash cannot yield a published m.db with no record.
    # Lives here (not in writer/) because the line carries a wall-clock
    # timestamp and writer/ is under the no-wallclock determinism gate.
    if report.provenance is not None:
        from rb2engine.report import ENGINE_LIBRARY_DIRNAME, append_journal

        try:
            journal = append_journal(Path(drive) / ENGINE_LIBRARY_DIRNAME, report.provenance)
            click.echo(f"journal: {journal}")
        except OSError as exc:
            click.echo(
                f"warning: m.db was published but the provenance journal "
                f"could not be written: {exc} — a later verify will report "
                "missing provenance for this publish.",
                err=True,
            )

    _emit_report(report, drive, report_path)
    counters = report.counters
    click.echo(
        f"converted {counters.tracks_converted} tracks and "
        f"{counters.playlists_converted} playlists -> {m_db}"
    )
    if counters.tracks_skipped:
        click.echo(f"{counters.tracks_skipped} track(s) skipped — see the report")
        ctx.exit(1)
    ctx.exit(0)


@main.command("verify")
@click.argument(
    "drive",
    type=click.Path(exists=False, path_type=Path),
    required=False,
)
@click.option(
    "--sample",
    type=int,
    default=None,
    help="Check only the first N tracks (a full library over USB is slow).",
)
@click.option("--no-artwork", is_flag=True, help="Skip artwork comparison.")
@click.pass_context
def verify_cmd(
    ctx: click.Context, drive: Path | None, sample: int | None, no_artwork: bool
) -> None:
    """Decode the written m.db and diff it against a fresh parse of the source.

    Turns "I checked a few tracks in Engine" into a mechanical check across the
    whole library: beatgrid markers, cue pads, colours, labels and loop points
    are compared at sample granularity.

    Read-only. Exit codes: 0 everything matches, 1 discrepancies found,
    2 could not verify (no library, unreadable, unsupported schema),
    3 comparison not attributable — the m.db was built from a different
    export.pdb than the one on the stick; re-run convert. Real defects
    (broken chains, undecodable blobs) still exit 1 even then, and 2 keeps
    precedence over both.
    """
    if drive is None:
        raise click.UsageError("DRIVE is required (the mount point of the stick)")

    from rb2engine.verify import verify_library

    try:
        result = verify_library(drive, with_artwork=not no_artwork, sample=sample)
    except (FatalError, UnsupportedFormatError) as exc:
        click.echo(f"cannot verify: {exc}", err=True)
        ctx.exit(2)
    except Exception as exc:  # noqa: BLE001 - top-level guard: exit 2, never a traceback
        click.echo(f"verify failed: {exc}", err=True)
        ctx.exit(2)

    click.echo(result.render_text())
    # Partition lives on the result (verify.py) so the CLI cannot re-derive a
    # different contract from the one render_text just described.
    ctx.exit(result.exit_code)


@main.command("doctor")
@click.option(
    "--engine-db",
    type=click.Path(path_type=Path),
    default=None,
    help="Existing Engine m.db to check schema support for.",
)
@click.argument(
    "drive",
    type=click.Path(exists=False, path_type=Path),
    required=False,
)
@click.pass_context
def doctor_cmd(ctx: click.Context, engine_db: Path | None, drive: Path | None) -> None:
    """Report versions, bundled schema support, and what's on a drive.

    Run this first when something looks wrong, or before converting an
    unfamiliar stick. Strictly read-only — it never writes anything.

    Exit codes: 0 everything looks convertible, 1 something needs your
    attention (e.g. an unsupported schema).
    """
    from rb2engine.doctor import doctor_report

    try:
        result = doctor_report(engine_db=engine_db, drive_root=drive)
    except Exception as exc:  # noqa: BLE001 - diagnostics must never traceback
        click.echo(f"doctor failed: {exc}", err=True)
        ctx.exit(2)

    for line in result.lines:
        click.echo(line)
    ctx.exit(0 if result.ok else 1)


main.add_command(waveforms)


if __name__ == "__main__":  # pragma: no cover
    main()
