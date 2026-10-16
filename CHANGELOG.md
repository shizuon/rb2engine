# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.1] - 2026-10-16

### Fixed

- Retain macOS FAT/SMB special-character aliases only when the SFM spelling
  resolves to the same non-symlink file. Ordinary filenames are not rewritten.

- Preserve source Unicode spelling in portable track paths when macOS FAT
  enumeration decomposes names. Retain actual filename casing and require the
  proposed spelling to open the same file before storing it.

- Recover materially inaccurate duration estimates for unindexed VBR MP3 files
  only when a complete MPEG frame scan agrees exactly with decoded PCM. Keep
  indexed/native clocks and reject partial frames, skipped junk, stream changes,
  decode warnings and inconsistent sample counts.

### Changed

- Publish this continuation from `shizuon/rb2engine`, preserving the previous
  fork history and GitHub-only package releases.

## [0.6.0] - 2026-10-10

First release of the Last-Xuan-ai fork, based on upstream 0.5.0.

### Added

- Optional `waveforms` commands for three-band/RGB overview generation, cached
  batches, existing-library preparation, publication planning, apply/check and
  conflict-aware restore. Audio is reused; no second music library is created.
- Native helpers build in a user cache. The NumPy extra and external FFmpeg tools
  are opt-in; AAC additionally uses locally installed FFmpeg development libraries.
- Anonymized validation for two independent 50-song tests and integration
  regression against 220 revealed native references. Hardware playback remains
  unverified; native evidence is from Apple Silicon macOS.
- GitHub-only releases with wheel, source archive and checksums; no upstream
  PyPI publishing. CI retains the nine OS/Python combinations and adds native
  Apple Silicon integration.

### Fixed

- Accept Lexicon compact final PDB index groups only when the declared used heap
  justifies the boundary; retain malformed-index and row-count checks.
- Preserve timestamp corrections in constant-BPM grids within one millisecond
  using linear-time compression. Keep existing tempo-change anchors and place
  the final marker strictly after track end, including exact whole-beat ends.
- Select the first audio stream consistently when probing and decoding a
  multi-audio AAC file, including when another stream is marked default.
- Decode ID3v2.4 frame unsynchronisation before grouping/DLI fields, preserving
  stored frame boundaries and rejecting malformed or unsupported APIC payloads.

### Changed

- Conversion and waveform publication share a persistent process lock; failed
  first conversions retain only the lock when no database was published.
- Playlist chain reconstruction is linear instead of quadratic, with unchanged
  ordering and corruption checks.
- Waveform publication currently requires Engine schema 3.0.2. Backend builds
  target macOS/Linux; Windows base conversion remains supported, while waveform
  generation on Windows fails explicitly.

## [0.5.0] - 2026-08-01

0.4.0 stopped a wrong database from being published. This release answers the
question that made the original incident take days to diagnose: **when `convert`
and `verify` disagree, which one is out of date?**

### Added
- **Every `m.db` now records the bytes it was built from.** `convert` fingerprints
  `export.pdb` (sha256, size, mtime) as it parses it — hashing the buffer it
  actually read, not a re-read, so a torn read is fingerprinted *as torn* — and
  stores that alongside the published database's own hash and its id watermarks.

  `verify` compares provenance first and can now say **which oracle moved**: the
  source changed, the database changed, both, or neither. The incident that
  motivated all of this was verify blaming the database when the source had moved.
- **An append-only journal**, `Engine Library/rb2engine-journal.jsonl`, one line
  per conversion, capped at 64 KB. The report alone was not enough: "re-run
  convert" is exactly the remedy verify prescribes for staleness, and it overwrites
  the fixed-name report — destroying the evidence for the next incident the same
  way it was destroyed for the last one.
- **Engine DJ's own playlists are named as such.** Opening Engine DJ with a
  populated desktop library merges its playlists onto the stick. verify previously
  reported that identically to corruption. It is now reported as an informational
  **external edit**, naming each playlist, and does not fail verification.

  The discriminator is `lastEditTime`: rb2engine pins every playlist row it writes
  to the epoch, so any other value came from Engine. That survives Engine
  reassigning ids — measured on a real stick, ids ran to 84 against our contiguous
  1–45, which is why the id watermark alone is not usable.

### Changed
- **`verify` gained exit code 3, "not attributable".** When the recorded
  fingerprint does not match the source present now, source-dependent comparisons
  have no oracle and become informational.

  Source-*independent* findings — a broken `nextEntityId` chain, an undecodable
  blob — still force **exit 1** even under a mismatch. A stale source must never
  launder a real fault into "just re-run convert". Scripts that treat any non-zero
  exit as failure are unaffected; scripts that distinguish 1 from other codes
  should be updated.
- **A conversion whose report cannot reach the stick now says so loudly.** This
  was found the hard way: a convert reported success with its drive unmounted and
  quietly wrote its report into the user's source directory.

### Fixed
- Documented, after measuring on a 3,673-track library, that the reader's 13-bit
  page row count is **correct** and must not be "fixed" to the crate-digger shape.
  14 of 997 pages carry 284 rows and parse correctly; the byte at +24 is merely
  `284 & 0xFF`, and the u16 at +34 is not a row count at all. Implementing the
  spec as written would have broken a working parser on exactly the large
  libraries this tool exists for.

### Notes
- A stick converted before 0.5.0 has no provenance record. verify reports that
  visibly but **does not** fail for it — an existing clean library still exits 0.
  Re-run `convert` to start recording.

## [0.4.0] - 2026-07-31

Verified end-to-end on a real 3,673-track stick before release: `convert` exit 0
(3,673/3,673 tracks, 45/45 playlists, 0 skipped) followed by `verify` reporting
0 discrepancies. That is the same drive and the same `export.pdb` that produced
the phantom playlist entries this release exists to prevent.

### Added
- **`convert` now refuses to publish a database that disagrees with its own
  source.** Before the new `m.db` is swapped into place, every playlist's
  membership, order and entry chain is recomputed from the source and compared
  against what was actually written. On any disagreement the conversion fails
  with exit 2 and your existing library is left byte-for-byte intact.

  This is deliberately *not* the check added in 0.3.2. That one compares the
  database against what the writer intended, and both sides of it derive from
  the same track id map — so a mapping fault agrees with itself and passes. The
  new check recomputes each expected track id from the source track through the
  mapper and the database's own path index, never consulting that map, and so
  fails exactly where the older gate cannot.

  Scope, stated plainly: it is playlist-scoped, not a full verify, and it cannot
  tell you the source *file* was misread — both sides descend from the same
  parse. That is the reader's job (see G1d above). `rb2engine verify` remains
  the field-level check.

### Fixed
- **G1d — refuse a torn `export.pdb` instead of converting it.** A conversion on
  a real stick published two playlist entries that the settled `export.pdb` does
  not contain. Forensics on the drive established what happened: rekordbox last
  wrote the pdb at 12:09:38 UTC, `convert` started 14 seconds later at 12:09:52,
  and the file has not been modified since — so `convert` and the `verify` that
  caught it read the *same* file, and the difference arose while reading a pdb
  that was still settling. A stale slot read as present parses cleanly, lands in
  a coherent chain, and is indistinguishable downstream from a real entry.

  The page header already carries the contradiction: it declares `num_rows`, and
  the reader decoded that field and threw it away. The parser now checks that the
  present-bit count matches `num_rows`, and that every row offset points into the
  heap between the page header and the backward-growing row index. Both invariants
  hold on all 997 data pages of a real 3,673-track export, and ordinary deleted
  slots — 104 of them in that same file — still parse normally.

  Scope, stated plainly: this catches the demonstrated signature class. A torn
  image whose header was written before the pages it describes satisfies both
  checks and would still pass.
- `verify` paired each source playlist with the wrong Engine list in three ways,
  every one of which invents discrepancies on a correct conversion — the failure
  mode that trains you to ignore verify. Same-named playlists in different
  folders collapsed onto one list (Engine's uniqueness constraint is
  per-parent, so this is legal and common); duplicates within one folder all
  compared against the first, because the writer renames the second to
  `"Name (2)"` while both source lists keep the original name; and a missing
  playlist could resolve to an unrelated one whose title merely started the
  same way, so `"House"` was verified against `"House (old)"`.
- Playlists are now paired on their full folder path. Engine's
  unique-name-per-folder renaming lives in one shared module used by both the
  writer that applies the names and the verifier that has to predict them —
  verify re-deriving that by hand is what produced all three defects.

### Changed
- **Breaking (text output).** Playlist discrepancies are keyed by folder path
  rather than bare name: `playlist[Sets/Setlist].track_order`, previously
  `playlist[Setlist].track_order`. Nothing parses these keys programmatically,
  but scripts grepping verify's output will need updating.
- A playlist retitled in the database is now reported as missing instead of
  being silently matched when its new title resembles a duplicate suffix. This
  is divergence from the source and belongs in the report; classifying it as an
  external edit rather than an absence is follow-up work.

## [0.3.2] - 2026-07-31

### Fixed
- `convert` no longer publishes a database whose playlist chains disagree with
  what it meant to write. A conversion on a real stick published two playlists
  that each contained one track appearing nowhere in the corresponding source
  playlist — and `convert` still exited 0. Only a later `verify` found it. The
  root cause is *not* fixed here; it has not been identified, and the failure
  did not recur across four faithful rebuilds. What is fixed is that this class
  of corruption could be published silently. The check runs twice: inside the
  writing transaction, and again on the copy that actually crossed to the
  target volume, after fsync and before `os.replace`. A failure there leaves
  your previous `m.db` byte-for-byte intact.
- `verify` and the writer no longer disagree about what a valid
  `PlaylistEntity` chain is. verify walked a broken chain, returned whatever it
  reached, and reported the library clean — so verify would pass a database
  that `convert` would refuse to write. One walker (`rb2engine/chain.py`) now
  backs both, and a chain problem is reported in its own right as a
  `playlist[NAME].chain` discrepancy, because a broken chain is a defect even
  when the set of tracks still matches what the source expected.

### Testing
- 688 tests, 87% branch coverage.

## [0.3.1] - 2026-07-25

### Fixed
- Unicode path matching. `export.pdb` stores paths composed (NFC), while the
  same names read back off the stick arrive decomposed (NFD), so every track
  with an accented character anywhere in its path resolved to nothing and was
  reported as `skipped: resolved_path is None` — while the file sat there,
  present and playable. On a real 3,666-track stick that was 46 tracks. The
  filesystem hid it: macOS resolves NFC against NFD inside the syscall, so
  `exists()` on the very same path returned true.

### Added
- `convert` reports progress per phase on stderr. Disabled when stderr is not a
  TTY and under `--log-json`, which owns that stream.

## [0.3.0] - 2026-07-25

### Added
- Fresh sticks now adopt the schema from your own Engine desktop library when
  one is readable, instead of always falling back to a fixed default. A drive
  that already has a library still keeps its own version.
- `verify` now compares album-art **bytes**, not just AlbumArt row counts.
  Swapped, truncated or re-encoded images keep the count identical while the
  DJ sees the wrong cover.
- Coverage measurement with an 85% floor, enforced in CI and on release.

### Changed
- README documents `verify` and `doctor`, and the schema precedence rules.

### Testing
- 640 tests, 87% branch coverage (was 476 / 81% statement).
- `reader/tags.py` 52% -> 98%, `reader/library.py` 32% -> 98%,
  `writer/build.py` 77% -> 99%, `writer/artwork.py` 70% -> 100%.
  `library.py` mattered most: it is the orchestration join whose absence broke
  the pipeline once, and its only real tests were hardware-gated and skipped
  in CI.

## [0.2.0] - 2026-07-25

### Added
- `rb2engine verify` — decodes the written `m.db` and diffs it against a fresh
  parse of the source at sample granularity: beatgrid markers, cue pad index,
  ARGB colour, label, loop points, playlist order and artwork counts. Turns
  spot-checking a few tracks in the GUI into a mechanical check across the
  whole library. Exits 1 on any discrepancy.
- `rb2engine doctor` — reports tool and dependency versions, bundled schema
  support, and what is actually on a drive. Given an unsupported Engine
  schema it names the version and points at the capture runbook rather than
  failing bare. Strictly read-only.
- `CONTRIBUTING.md`, `docs/TROUBLESHOOTING.md`, and a schema-capture runbook
  in `writer/ddl/README.md`.

### Fixed
- Gate G1c's malformed *optional consumed* table branch (e.g. a corrupt
  `artists` table) had no pinning test — silently losing every artist name is
  exactly the plausible-looking-wrong-output failure the design guards
  against. Now byte-patched and tested.
- G1a: a pdb header listing the same consumed `page_type` twice was previously
  accidental behaviour. Now an explicit first-wins-with-warning policy.
- `test_no_wallclock` only grepped `writer/`, but time-derived values are
  wired through `mapper/track.py` too — half the surface was unchecked, which
  would have made determinism flaky across a second boundary.
- Converting on macOS left a `._m.db` AppleDouble sidecar in the user's
  Engine Library. Removed surgically (only our own file's sidecar, never a
  blanket `._*` sweep), and a no-op off macOS.

### Changed
- The stale "unimplemented command" test is replaced by one asserting the CLI
  has no stubs at all — it had gone stale three times as commands landed.

## [0.1.0] — 2026-07-25

First public release. Converts a rekordbox USB export into an Engine DJ library
on the same stick, referencing audio in place rather than copying it.

**Verified end-to-end** on a real **3,665-track** library: **3,620** tracks,
**47** playlists, beatgrids, hot cues, loops and album art, with playback
confirmed in **Engine DJ 4.3.0** from the stick. Nothing outside
`Engine Library/` is written.

### Added

- **Reader pipeline** for rekordbox USB exports:
  - Hand-written `export.pdb` parser (`construct`) with DeviceSQL strings,
    paged table walk, and path resolution suitable for FAT32 sticks
  - ANLZ analysis reader (beatgrids, hot cues, memory cues, loops) via a
    per-tag allowlist walk that survives tags pyrekordbox cannot parse
  - Drive layout scan with case-insensitive matching; artwork extraction from
    embedded tags (MP4 / ID3 / FLAC) without a GPL runtime dependency
- **Intermediate representation** (`ir.py` / `ir_engine.py`) that fully
  decouples rekordbox types from the Engine writer
- **Mappers** for keys (Camelot / Open Key / enharmonics), cue-to-pad policy
  (hot cues keep pads; memory cues fill remaining slots; loops free pads),
  and beatgrid compression
- **Writer pipeline** for Engine Library `Database2/m.db`:
  - PerformanceData blob codecs (`trackData`, `beatData`, `quickCues`,
    `loops`) validated by **byte-identity against Engine's own output**
    (`encode(decode(blob)) == blob`)
  - Schema DDL **captured from Engine-authored databases** (not transcribed
    from libdjinterop): triples **3.0.1** and **3.0.2**
  - Atomic local-stage build + `os.replace` install; sibling DBs (`hm.db`,
    `sm.db`, `stm.db`) and `Music/` / `Artwork/` / `OverviewData/` preserved
  - Playlist tree with deterministic rename of duplicate sibling names and
    de-duplication of repeated track membership (Engine schema constraints)
- **CLI** (`rb2engine`):
  - `convert` — full conversion onto the stick
  - `inspect` — read-only library dump
  - Flags: `--dry-run`, `--no-artwork`, `--path-base`, `--target-schema`,
    `--database-uuid`, `--report`, logging controls
  - JSON conversion report under `Engine Library/rb2engine-report.json`
- **Packaging**: MIT-licensed runtime deps only; installable from PyPI as
  `rb2engine`; CI matrix macOS / Windows / Ubuntu × Python 3.11–3.13

### Notes

- The same Engine DJ build can run different schema versions on desktop vs
  stick; the target triple is read from the existing `m.db` (or
  `--target-schema`), never assumed from the app version string.
- Waveforms are not generated — Engine analyses them on load. Beatgrids, cues
  and loops are what must survive conversion.
- `verify` and `doctor` commands are reserved stubs in this release.

[0.1.0]: https://github.com/jrgutier/rb2engine/releases/tag/v0.1.0
