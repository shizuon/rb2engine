# rb2engine — shizuon fork

[![CI](https://github.com/shizuon/rb2engine/actions/workflows/ci.yml/badge.svg)](https://github.com/shizuon/rb2engine/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/shizuon/rb2engine)](https://github.com/shizuon/rb2engine/releases)

This fork builds on [J.R. Gutierrez's rb2engine](https://github.com/jrgutier/rb2engine).
Version 0.6.1 retains the verified overview generation and conversion fixes from
[the previous fork](https://github.com/Last-Xuan-ai/rb2engine), and adds conservative
unindexed VBR duration recovery and portable Unicode/SFM path handling.
Special-name aliases are used only when they resolve to the same file; this does
not rename audio or certify Windows application or physical-player compatibility.
The upstream PyPI package remains separately maintained; install this fork from its
[GitHub releases](https://github.com/shizuon/rb2engine/releases) or source.

Convert a **rekordbox** USB export into an **Engine DJ** library — on the same stick, in place, without duplicating a single audio file.

The converted library contains tracks, playlists, beatgrids, hot cues, loops and
artwork. The Pioneer export and audio remain unchanged. The new overview feature
has desktop validation; hardware playback of this fork still needs validation.

```bash
rb2engine convert /Volumes/MY_USB
```

## Desktop app

A small Chinese/English window: choose a USB, read its library, convert, verify.
Windows x64 and macOS Apple Silicon builds are produced by
[Desktop builds](https://github.com/shizuon/rb2engine/actions/workflows/desktop.yml).
See [desktop setup and compatibility](docs/desktop.md) for downloads, WebView2,
startup logs and database recovery copies. CI verifies the frozen app; physical
USB/player acceptance remains separate.

```bash
uv sync --extra desktop
uv run rb2engine-gui
```

## Why this exists

Engine DJ can already import a rekordbox stick — but its export **copies your audio** into `Engine Library/Music/`. On a full stick that means a second copy of your whole library. A 44 GB library needs 44 GB free, and usually there isn't.

rb2engine writes the library and its assets under `Engine Library/`. Your music
stays where rekordbox put it, and both systems index the same files:

```
USB STICK
├── Contents/            ← your audio, untouched, referenced by BOTH
├── PIONEER/             ← rekordbox's export, never modified
└── Engine Library/
    ├── Database2/m.db
    └── Database2/OverviewData/  ← optional RGB overviews
```

Upstream reported verification on a real 3,665-track library: **3,620 tracks,
47 playlists, ~500 MB of database** against a 44 GB library, without a second
audio copy. The fork's overview evidence is reported separately below.

## What transfers

| | |
|---|---|
| Tracks | title, artist, album, genre, label, comment, composer, remixer, year, track/disc number, BPM, key, rating, bitrate, length |
| Playlists | full folder hierarchy and track order |
| Beatgrids | including manually adjusted grids and tempo changes |
| Hot cues | same pad numbers (A–H), with custom colours and names |
| Memory cues | fill remaining pads in chronological order |
| Saved loops | in/out points, into Engine's separate loop slots |
| Album art | extracted from your files' embedded tags, deduplicated |
| Overview waveforms | optional 1,024-point three-band and RGB generation from existing audio |

## Install

```bash
python -m pip install "rb2engine @ git+https://github.com/shizuon/rb2engine.git@v0.6.1"
```

Requires Python 3.11+. Base conversion works on macOS, Windows and Linux. The
optional waveform backend compiles locally on macOS and Linux. Desktop packages
include the waveform runtime on macOS ARM64 and Windows x64, with no compiler or
FFmpeg installation required. Broad native overview comparisons were performed
on Apple Silicon macOS; bundled-runtime checks do not establish every-song or
physical-player parity on every platform.

To install from a release artifact, download the wheel from this fork's Releases
page and run `python -m pip install /path/to/rb2engine-0.6.1-py3-none-any.whl`.

From source:

```bash
git clone https://github.com/shizuon/rb2engine
cd rb2engine
uv sync
uv run rb2engine --help
```

## Usage

```bash
# Check your setup and what's on a stick — always safe to run first
rb2engine doctor /Volumes/MY_USB

# Look at what rekordbox recorded — reads only, writes nothing
rb2engine inspect /Volumes/MY_USB

# Convert
rb2engine convert /Volumes/MY_USB

# Confirm the conversion is faithful, track by track
rb2engine verify /Volumes/MY_USB

# See what would happen without writing
rb2engine convert /Volumes/MY_USB --dry-run

# Skip artwork (much faster — artwork reads every audio file)
rb2engine convert /Volumes/MY_USB --no-artwork
```

A conversion of a full stick takes minutes, so `convert` reports each phase as
it goes:

```
reading tracks ▰▰▰▰▰▰▰▰▰▰▰▰▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱  30%
```

Progress goes to stderr and switches itself off when stderr is redirected or
`--log-json` is set, so piping stays clean.

### Commands

| Command | Writes? | What it does |
|---|---|---|
| `convert` | **yes**, only inside `Engine Library/` | Builds the Engine library from the rekordbox export |
| `inspect` | no | Dumps what rekordbox recorded — tracks, playlists, cues, grids. `--json` for machine output |
| `verify` | no | Decodes the library you just wrote and diffs it against the source |
| `doctor` | no | Versions, supported schemas, and a read of the drive |
| `waveforms` | depends on subcommand | Opt-in generation, publication planning, apply/check/restore; see below |

**Base command exit codes**: `0` all good · `1` finished with something worth
your attention — tracks skipped on `convert`, discrepancies on `verify` · `2`
fatal, nothing usable written. `verify` also uses `3` when changed source
provenance prevents attribution. Waveform commands use `1` for dependency,
generation or publication failures and `2` for CLI usage errors.

Every `convert` writes `Engine Library/rb2engine-report.json` listing what
converted, what was skipped and why, and any dropped cues or loops.

### Checking a conversion worked

`verify` is the answer to "did that actually work?". Instead of opening Engine
and spot-checking a few tracks, it decodes the `m.db` that was written and
compares it against a fresh parse of the source — **at sample granularity**:

- beatgrid marker positions
- hot-cue pad number, position, ARGB colour and label
- loop in/out points
- track metadata and that each `path` resolves to a real file
- playlist order, reconstructed from Engine's linked list
- artwork counts

```bash
rb2engine verify /Volumes/MY_USB            # whole library
rb2engine verify /Volumes/MY_USB --sample 50  # first 50 tracks (much faster over USB)
```

Exit `0` if everything matches, `1` if it finds discrepancies (each one listed
with track, field, expected and actual), `2` if it could not verify at all, or
`3` when the recorded source has changed and comparisons are not attributable.
`verify` checks converted metadata; use `waveforms check` for a waveform publication.

### Add overview waveforms

Install the extra plus external tools first; macOS needs Xcode Command Line Tools,
FFmpeg and pkg-config. See [complete setup and commands](docs/waveforms.md).

```bash
python -m pip install "rb2engine[waveforms] @ git+https://github.com/shizuon/rb2engine.git@v0.6.1"
brew install ffmpeg pkgconf
rb2engine waveforms doctor

# Convert first if this drive has no Engine library. Waveform publication needs 3.0.2.
rb2engine convert /Volumes/MY_USB --target-schema 3.0.2

# Reuse existing audio and create a reviewable publication plan.
rb2engine waveforms prepare /Volumes/MY_USB \
  --out "/Volumes/MY_USB/Engine Library/rb2engine-waveforms/first-pass"

# Read generation.json and publication/plan.json, then quit Engine DJ.
rb2engine waveforms apply "/Volumes/MY_USB/Engine Library/rb2engine-waveforms/first-pass/publication"
rb2engine waveforms check "/Volumes/MY_USB/Engine Library/rb2engine-waveforms/first-pass/publication"
```

For a library that already exists, start with `waveforms prepare`; re-running
`convert` rebuilds the converted database and is not a waveform repair command.
Generation errors prevent a publication plan by default. `--allow-partial`
explicitly prepares only successful tracks and still exits `1` so failures stay visible.

The standalone algorithm passed **100/100 previously unseen songs** in two frozen
50-song tests: 49 exact and 51 near. The integrated implementation was also compared
against all 220 revealed native references; see the
[validation scope and criteria](docs/waveform-validation.md) and
[integration regression evidence](docs/integrated-waveform-validation.json).
These results cover stereo MP3, AAC, FLAC and PCM16 WAV at 44.1/48 kHz on Apple
Silicon macOS. They do not establish hardware playback or every decoder/platform.

### When something looks wrong

`doctor` first. It is read-only and tells you what rb2engine sees:

```bash
rb2engine doctor                      # versions and supported schemas
rb2engine doctor /Volumes/MY_USB      # + what's actually on the drive
rb2engine doctor --engine-db path/to/m.db   # is this schema supported?
```

It reports the tool and dependency versions, which Engine schemas are bundled,
the drive layout, and — if the drive already has a library — its schema and
UUID. An unsupported schema is named explicitly along with how to add it,
rather than failing with a bare error. See
[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

### Options

| Flag | Purpose |
|---|---|
| `--dry-run` | Parse and map, write nothing |
| `--no-artwork` | Skip cover-art extraction |
| `--target-schema 3.0.2` | Force an Engine schema version (see *Which schema gets written* below) |
| `--database-uuid UUID` | Override the library UUID (default: reuse the existing one) |
| `--report PATH` | Write the JSON report elsewhere |
| `-v` / `-vv` | More logging · `--log-json` for machine-readable logs |

## Safety

rb2engine is built to be non-destructive, and the guarantee is tested rather than asserted:

- **On the target drive, library changes stay inside `Engine Library/`.** Conversion tests check the drive tree before and after. Waveform generation also uses a separate user cache for native helpers and derived files; explicit output/report paths are controlled by the caller.
- **Your audio is never opened for writing.** Artwork extraction is read-only, and a test hashes source files before and after to prove it.
- **Conversion uses an atomic database swap** against process failure. Waveform publication updates SQLite and separate RGB files with backups and a recovery journal; those two stores cannot form one atomic transaction. `waveforms restore` provides recovery for a prepared batch.
- **Conversion preserves Engine's auxiliary files.** `hm.db` (your play history), `sm.db`, `stm.db`, `Music/`, `Artwork/` and `OverviewData/` survive conversion. Waveform publication replaces only its selected overview files. The library UUID is carried forward so Engine's history stays linked.

Still: **it's a DJ library. Back it up before you point a new tool at it.**

## Things worth knowing

**Re-running `convert` rebuilds from scratch.** The Engine library is derived data. Edits made *in Engine* to the converted library are not preserved — re-export from rekordbox, re-run, and you get a clean library.

**Two rekordbox habits Engine can't represent.** Both are handled rather than fatal, and both appear in the report:

- *Duplicate playlist names in one folder.* rekordbox allows it; Engine's schema doesn't. Duplicates get a numeric suffix (`Setlist (2)`), assigned deterministically so re-runs are stable.
- *The same track twice in one playlist.* rekordbox allows it; Engine doesn't. The first occurrence is kept.

**More than 8 cues on a track.** Engine has 8 pads. Hot cues keep their original pad; memory cues fill what's left chronologically; anything beyond 8 is dropped and itemised per-track in the report. Loops go to Engine's separate 8 loop slots, so a looped hot cue frees its pad.

**Overview generation is opt-in.** `convert` keeps its existing role.
`waveforms prepare` generates the list preview and whole-track overview, preserving
BPM, key, cue, loop and grid data. It does not generate the scrolling high-resolution
waveform or analyze BPM/grid. The desktop app can optionally fill missing keys
using a separately installed [OpenKeyScan](https://www.openkeyscan.com/download).
Existing keys are preserved. Engine provides its own analysis for other data.

## Supported versions

- **Base conversion**: Engine schema `3.0.1` and `3.0.2`.
- **Waveform publication**: schema `3.0.2` only; native reference checks used Engine DJ 5.1.0.
- **rekordbox**: exports from rekordbox 5, 6 and 7.

### Which schema gets written

Never guessed from the app version — the same Engine build was observed running
`3.0.1` on the desktop library and `3.0.2` on a USB stick. In precedence order:

1. `--target-schema`, if you pass it
2. **the schema already on the drive** — a `3.0.2` stick stays `3.0.2`
3. your own Engine desktop library, if one is readable on this machine
4. `3.0.1` as a conservative fallback

The fallback is the *oldest* supported version on purpose. Engine migrates an
older schema upward — a `3.0.1` stick was migrated in place to `3.0.2` by
Engine DJ 4.3.0 — but there is no downgrade path, so writing the newest version
to a fresh stick could hand an older Engine something it cannot open.

If your stick uses a schema rb2engine doesn't know, it **refuses to write** and tells you, rather than producing a database Engine might silently misread. Adding a version is a schema capture plus one line — see `src/rb2engine/writer/ddl/README.md`.

## How it works

```
export.pdb ────┐
               ├─→ Source IR ─→ mapper ─→ Engine IR ─→ Engine Library/Database2/m.db
ANLZ .DAT/.EXT ┘
```

- `reader/` parses `export.pdb` (DeviceSQL, via `construct`) and the ANLZ analysis files (beatgrids, cues, loops)
- `ir.py` / `ir_engine.py` decouple the two sides — no rekordbox type reaches the writer, no Engine type reaches the reader
- `mapper/` applies the semantics: key ordinals, cue-to-pad policy, beatgrid compression
- `writer/` builds the SQLite database from schema DDL captured from Engine's own output

Positions are integer sample counts throughout; the millisecond conversion happens exactly once, at the reader boundary.

## Development

```bash
uv sync --all-extras --dev
uv run pytest -m "not real_stick"
uv run ruff check src/ tests/
uv run mypy src/
```

Tests marked `real_stick` need an actual rekordbox USB mounted and are skipped otherwise:

```bash
uv run pytest -m real_stick
```

The blob codecs are validated by **byte-identity against Engine's own output**: `encode(decode(blob)) == blob` for `beatData`, `quickCues`, `loops` and `trackData`, using a database Engine itself wrote. That check is what makes the encoders trustworthy.

## License

MIT — see [LICENSE](LICENSE).

The Python dependencies are permissively licensed. The optional waveform feature
also executes separately installed FFmpeg tools and builds an AAC helper that
dynamically links the user's FFmpeg libraries; FFmpeg's LGPL/GPL terms depend on
that build. Release artifacts contain project source, not FFmpeg binaries or
Engine private libraries. See [NOTICE](NOTICE) for the exact scope and attribution.

rb2engine is an independent interoperability tool, unaffiliated with AlphaTheta/Pioneer DJ or inMusic/Denon DJ.
