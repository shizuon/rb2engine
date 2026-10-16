# Contributing to rb2engine

Thanks for helping. This project converts real DJ libraries; wrong bytes are
worse than missing features. The testing rules below are load-bearing, not
style preference.

## Development setup

Requires **Python 3.11+** and [uv](https://github.com/astral-sh/uv).

```bash
git clone https://github.com/Last-Xuan-ai/rb2engine
cd rb2engine
uv sync --all-extras --dev
```

Run the unit suite (default; no hardware required):

```bash
uv run pytest
```

Lint and type-check:

```bash
uv run ruff check src/ tests/
uv run mypy src/
```

Optional formatting check (avoid reformatting unrelated files):

```bash
uv run ruff format --check src/ tests/
```

Install an editable CLI for manual runs:

```bash
uv run rb2engine --help
uv run rb2engine convert --help
```

## Project layout

| Path | Role |
| --- | --- |
| `src/rb2engine/reader/` | rekordbox USB export → source IR |
| `src/rb2engine/ir.py`, `ir_engine.py` | Decoupling boundary — no cross-imports of side types |
| `src/rb2engine/mapper/` | Semantics (keys, cues, beatgrids, track fields) |
| `src/rb2engine/writer/` | Engine IR → `m.db` (DDL, blobs, tracks, playlists) |
| `src/rb2engine/overviews/` | Optional decoder/DSP, verified caches and waveform publication |
| `src/rb2engine/writer/ddl/` | Engine-authored schema captures — see that README |
| `tests/unit/` | Fast tests; golden fixtures under `tests/fixtures/` |
| `tests/fixtures/golden/` | **Read-only** Engine-authored artifacts — never overwrite |

## Testing philosophy

These rules exist so a self-consistent-but-wrong encoder cannot ship.

### 1. Blob codecs: byte-identity against Engine

PerformanceData codecs are validated by round-tripping **Engine's own bytes**:

```text
encode(decode(blob)) == blob
```

for `beatData`, `quickCues`, `loops`, and `trackData`, using databases Engine
itself wrote (`tests/fixtures/golden/engine_ref.db` and related fixtures).

A codec that invents a private dialect can still be internally consistent.
Byte-identity against Engine is what makes the encoders trustworthy. If you
change a codec, the golden round-trip must stay green; if Engine changes
framing, capture new golden blobs — do not weaken the gate.

### 2. Never generate expectations from the implementation

**Do not** run your own code and paste the output as the expected value.

Expected values come from only three places:

1. **The specification** (documented format rules, Engine constraints, explicit
   product decisions in code comments / plan)
2. **Engine-authored fixtures** (golden `m.db` files, real pdb slices under
   `tests/fixtures/real_bytes/`)
3. **Hand-authored reasoning** (small constructed inputs where every field is
   justified in the test docstring)

Self-generated expectations are tautological: they only prove the code agrees
with itself. Mutation-test the gate when you can (e.g. flip an endianness
constant and confirm the golden test fails).

### 3. `real_stick` tests need physical hardware

Tests marked `@pytest.mark.real_stick` require a mounted rekordbox USB export
and are **skipped** otherwise (including in CI):

```bash
uv run pytest -m real_stick
```

Do not rewrite them to pass without hardware. Do not commit paths or personal
library metadata from a production stick into fixtures — use the Tier-A
authoring checklist under `tests/fixtures/AUTHORING.md` for committable data.

### 4. Non-destructive guarantee is tested, not asserted

On the target drive, library changes must stay inside `Engine Library/`.
Native helpers and generated waveform caches use a separate user cache. Explicit
report/cache destinations are caller-controlled and must never overwrite inputs.

That is enforced by tests that walk the drive tree before and after a
conversion (and by related checks that source audio hashes are unchanged after
artwork extraction). If you add a write path, extend those tests. A comment
that says “we never touch `PIONEER/`” is not evidence.

Default report path is under `Engine Library/` so the report itself does not
violate the boundary.

## Schema versions

Unsupported Engine schema triples fail loud. Adding support is a DDL capture
plus one dict entry — step-by-step in
[`src/rb2engine/writer/ddl/README.md`](src/rb2engine/writer/ddl/README.md).

After a capture:

```bash
uv run pytest tests/unit/test_schema.py -v
```

## Pull requests

1. Keep changes focused. Touch only what the change needs.
2. Prefer tests that encode **why** a behaviour matters (what breaks for a DJ
   if it regresses), not only that today's output matches itself.
3. Run `uv run pytest`, `ruff`, and `mypy` before opening the PR.
4. Do not commit secrets, full production libraries, or commercial album art.
5. Do not reformat unrelated files.

## License

Contributions are under the same MIT license as the project (see `LICENSE`).
Base Python dependencies must remain permissively licensed. Mutagen is a
**dev-only** oracle, never imported from `src/`. The optional waveform backend
has separately installed FFmpeg runtime dependencies: update NOTICE accurately
when changing that boundary, and do not bundle FFmpeg or private vendor binaries.

## Questions

Open a GitHub issue. For conversion failures on a real stick, attach the
`Engine Library/rb2engine-report.json` (redact paths if needed) and the schema
triple from `Information` if you can read it from a **copy** of `m.db`.

## Optional waveforms and release checks

Keep base imports and `--help` independent of NumPy, compilers and FFmpeg.
Regular PR checks and releases use the bundled Python 3.12: one Linux full
suite with Ruff, mypy and the 85% coverage floor, plus Windows/macOS desktop
jobs. The desktop jobs exercise platform file locks, Unicode paths, recovery,
native waveform references and the actual frozen UI, model and audio tools.
The frozen model check replaces the duplicate pre-freeze audio-model check.

Python 3.11/3.13 source suites and the external Apple Silicon FFmpeg/compiler
integration remain available in **Optional source compatibility**, started
manually when those support boundaries change. They are not desktop release
gates. PRs run checks once; merging does not repeat them. Tags rerun the core
suite and both desktop builds against the exact published source.

Use an isolated native cache for local testing when needed:

```sh
RB2ENGINE_OVERVIEWS_BUILD_CACHE=/tmp/rb2engine-build uv run pytest -m "not real_stick" --cov
```

Expectations for DSP tests must come from independent Engine output or explicit
mathematics. Do not weaken byte/time gates or label revealed-sample regression
as new independent validation. Public reports must omit private titles, paths,
audio hashes and databases. See [waveform validation](docs/waveform-validation.md).

This fork releases wheel/source archives and checksums through GitHub Releases.
The release workflow reuses CI, then builds with `uv build` and checks metadata
with `uvx twine check`. A version tag must match `pyproject.toml`; manual workflow
dispatch validates/builds without publishing. The workflow does not publish to
upstream PyPI or use its trusted-publisher identity.
