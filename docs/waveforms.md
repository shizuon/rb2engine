# Optional Engine overview waveforms

`rb2engine waveforms` creates 1,024-point three-band and RGB overviews from the
existing audio files. It adds list previews and whole-track overviews to an
existing Engine library without a second music copy. It does not generate the
scrolling high-resolution waveform, analyze BPM/key/grid, or change performance
markers. `convert` and waveform publication are separate operations.

## Install and check

Install this fork, not the separately maintained upstream PyPI release:

```sh
python -m pip install "rb2engine[waveforms] @ git+https://github.com/Last-Xuan-ai/rb2engine.git@v0.6.0"
```

For source development use `uv sync --all-extras --dev`. A downloaded wheel can
also be installed with `python -m pip install "/path/to/rb2engine-0.6.0-py3-none-any.whl[waveforms]"`.
The extra installs NumPy. Native helpers compile locally in a user cache, not
inside the installed package.

On macOS, install Xcode Command Line Tools and the external audio dependencies:

```sh
xcode-select --install
brew install ffmpeg pkgconf
rb2engine waveforms doctor
```

On Debian/Ubuntu, the corresponding packages are `build-essential`, `ffmpeg`,
`pkg-config`, `libavformat-dev`, `libavcodec-dev`, and `libavutil-dev`.
FFmpeg and ffprobe must be on PATH. AAC additionally needs development headers
and dynamically linkable libavformat/libavcodec/libavutil, discoverable through
pkg-config. `doctor --no-aac` checks the generic DSP without building the AAC
helper; it does not establish that AAC generation will work.

The backend builds on macOS and Linux. Native comparisons were performed on
Apple Silicon macOS; Linux is not covered by that native validation. Windows
waveform generation is explicitly unsupported, while base conversion remains
available there. Other FFmpeg/compiler builds can change decoder rounding.

An MP3 without a Xing/VBRI duration index can have a severely wrong bitrate-based
duration estimate. When FFmpeg reports this estimate and it differs from decoded
audio by more than one second or one percent (whichever is larger), generation
can recover the clock by scanning every MPEG frame and requiring its sample count
to equal the decoded PCM count. Recovery requires variable bitrate, constant
rate/channels, zero start offset and no other decoder warnings. Only leading
ID3v2 and trailing ID3v1 tags are supported in this fallback; unknown bytes or
partial frames fail. The cache records both the original estimate and the verified
frame count. Source audio is not rewritten. This proves consistency of the audio
present in the file; missing whole frames cannot be detected without an external
reference. The recovered clock has not been compared with Engine's native reader
for these unindexed long-set files.

## Existing-library workflow

Waveform publication requires schema **3.0.2** and recognizable existing
`trackData`. Use `rb2engine doctor DRIVE` to inspect the schema. When creating a
new library, run `rb2engine convert DRIVE --target-schema 3.0.2` first. Do not
re-run conversion simply to add waveforms: conversion rebuilds the database.

```sh
rb2engine waveforms prepare /Volumes/MY_USB \
  --out "/Volumes/MY_USB/Engine Library/rb2engine-waveforms/first-pass"
```

Omit `--track` to generate for every current Track, or repeat it to select Engine
Track IDs, for example `--track 12 --track 37`. Audio paths come from the existing
library. `--out` must be a new directory under that drive's `Engine Library`.
Without it, the command creates `Engine Library/rb2engine-waveforms/<UTC timestamp>`.

Read `generation.json` and `publication/plan.json`. Generation does not change
the database. The plan records source hashes, library identity, protected-field
fingerprints, intended waveform replacements and any exact-duration header change.
If any input fails, the default is no publication plan and exit 1.
`--allow-partial` explicitly permits a plan for successful inputs; it still exits
1 and retains failures in the generation report.

After reviewing the plan, quit Engine DJ and apply:

```sh
rb2engine waveforms apply "/Volumes/MY_USB/Engine Library/rb2engine-waveforms/first-pass/publication"
rb2engine waveforms check "/Volumes/MY_USB/Engine Library/rb2engine-waveforms/first-pass/publication"
```

Publication verifies the target UUID, schema, audio hashes, input files and
protected metadata. It can change only classic overview bytes, the corresponding
RGB file, and the first 16 decoded bytes of `trackData` (sample rate/exact length).
A sample-rate mismatch is rejected; cues and grids are never rescaled. Track
fields, key, cues, loops, grids and playlists are protected.

The tool saves a consistent database backup and original RGB bytes. SQLite and
external RGB files cannot share an atomic transaction, so a journal records the
publication state for recovery. Preserve the publication directory and cache.
To recover an interrupted write or undo this batch, quit Engine and run:

```sh
rb2engine waveforms restore "/Volumes/MY_USB/Engine Library/rb2engine-waveforms/first-pass/publication"
```

Restore targets this batch's waveform/timing changes and preserves unrelated
later edits. It rejects conflicting changes to the same waveform or protected
trackData bytes instead of replacing the current library with an old snapshot.
`check` uses the preparation-time protection baseline: subsequently loading a
track in Engine can itself add automatic cue or analysis metadata and cause a
reported difference. Inspect that difference rather than overwriting it.

## Single files, batches and explicit mappings

```sh
rb2engine waveforms generate /path/to/song.mp3 --cache /path/to/cache
rb2engine waveforms verify-cache /path/to/cache/content-entry
rb2engine waveforms batch audio.json --cache /path/to/cache --report batch-result.json
```

`audio.json` is a nonempty JSON array of audio path strings. Every song stays in
the batch report, including failures. Batch errors return 1. Resume with the
same cache and a new report path; complete verified cache entries are reused.
The first audio stream is selected consistently in files with multiple audio streams.
Reports are never silently overwritten. Cache identity includes audio content,
algorithm and runtime dependencies; corrupt cache entries fail verification.

For applications that already know the track-to-cache mapping, create a JSON
array such as `[{"track_id":12,"cache_path":"/path/to/cache/content-entry"}]`:

```sh
rb2engine waveforms plan "/path/to/Engine Library/Database2/m.db" mapping.json \
  --out "/path/to/Engine Library/rb2engine-waveforms/manual-plan"
```

Use that output directory with `apply`, `check` and `restore`. Unlike `prepare`,
`plan` consumes existing cache entries; it does not generate audio features.

`RB2ENGINE_OVERVIEWS_BUILD_CACHE` overrides the native-helper build directory.
Defaults are `~/Library/Caches/rb2engine/overviews/build` on macOS and
`${XDG_CACHE_HOME:-~/.cache}/rb2engine/overviews/build` on Linux. Generated
waveforms default to the sibling `waveforms` directory; `--cache` overrides it.
These are derived files, not audio-library copies. Temporary decoded PCM is
removed after generation.

## Validation and limits

The frozen standalone algorithm passed two independent 50-song tests, with both
waveforms required to pass for every song. The integration was then rerun against
all 220 revealed references. See [native criteria and coverage](waveform-validation.md)
and [integration regression data](integrated-waveform-validation.json). Regression
on revealed samples is not a new blind test.

Native evidence covers stereo MP3, AAC, FLAC and PCM16 WAV at 44.1/48 kHz.
The implementation accepts some additional PCM encodings and mono, but those
are outside the independent-song runs. Unsupported codecs/rates fail explicitly.
Hardware playback, other operating systems and arbitrary decoder builds are not
certified by these results. No private audio, vendor libraries or user database
is distributed in this repository. FFmpeg remains a separately installed runtime
dependency; see [NOTICE](../NOTICE).
