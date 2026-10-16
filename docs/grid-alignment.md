# Beatgrid alignment and downbeat numbering

By default rb2engine preserves the source grid. Version 0.8.0 adds an optional,
experimental audio check that can shift a fixed-tempo grid and its bar phase.
It does not infer a new tempo or create variable-tempo grids. Correct overview
waveforms alone do not establish that source beat times or bar labels are correct.

## Conversion mechanism

1. Rekordbox's PQTZ entries supply the timestamp in milliseconds, BPM metadata
   and a beat-within-bar value from 1 to 4.
2. The reader converts the timestamp to samples at the track sample rate.
3. The mapper compresses the dense positions into sparse Engine markers while
   bounding interpolation error. Engine reconstructs intermediate beats from
   the sample distance divided by the beat-number distance between markers.
4. Engine bar boundaries have a beat number divisible by four. The first
   marker is extrapolated to index -4 and the last past the audio end.
5. The writer stores the default and adjusted marker lists in `beatData`.
   Overview waveform data is generated and stored separately.

The Engine normalization convention is documented by the implementation in
[libdjinterop](https://github.com/xsco/libdjinterop/blob/main/src/djinterop/engine/engine.cpp).
[Mixxx's Engine export](https://github.com/mixxxdj/mixxx/blob/main/src/library/export/engineprimeexportjob.cpp)
likewise chooses a bar-aligned beat as index zero before normalization.

## The bar-phase defect fixed in 0.7.1

The previous mapper used the dense array index as the beat number, ignoring
the first entry's position within its bar. If the source began on beat 3,
every beat timestamp could remain correct while the bar emphasis was two
beats out of phase.

At 120 BPM, consider source entries at 0.25, 0.75, 1.25 and 1.75 seconds,
labelled 3, 4, 1 and 2. Their Engine beat numbers must be 2, 3, 4 and 5,
respectively. The downbeat is 1.25 seconds; choosing array indices 0, 1, 2
and 3 would incorrectly emphasize 0.25 seconds instead.

Version 0.7.1 preserves this initial bar phase. It does not move source beat
timestamps, cues or audio, nor does it infer a new tempo. Tests cover all four
initial bar positions and 44.1/48 kHz. The packaged-app fixture checks the
written database against the authored downbeat/Cue time, not just against a
second invocation of the mapper.

This correction addresses a specific conversion error. It does not prove that
every perceived first-beat problem has that cause, and it does not infer
intentional mid-track changes in meter or bar phase.

## Distinguishing symptoms

| Observed behavior | What to investigate |
|---|---|
| Beat lines land on attacks, but the emphasized bar line is 1–3 beats away | Source bar phase and the converter's beat numbering |
| Every line is early/late by roughly the same fraction of a beat | Source timing offset and decoded-audio time origin |
| Alignment gets progressively worse | Tempo error, half/double tempo interpretation, or source grid changes |
| Only some codecs show a small fixed error | Encoder priming/padding and the time origin used by each decoder |
| Grid and Cue data disappear together | ANLZ parse failures, including optional tags |

PQTZ's whole-millisecond resolution is a different issue from an entire beat
or bar being emphasized incorrectly. Optional PQT2 microsecond refinements are
not enabled in 0.7.1.

## Optional grid repair (0.8.0)

Enable **Try grid offset repair (experimental)** in the desktop app, then choose
**Strict** (default) or **Relaxed**. The switch itself is off by default. The
model is bundled, runs on the CPU offline, and does not require OpenKeyScan or
PyTorch. Source/wheel users can install the `grid-repair` extra and provide
FFmpeg/ffprobe (plus the existing AAC helper dependencies for AAC):

```sh
python -m pip install "rb2engine[grid-repair] @ git+https://github.com/shizuon/rb2engine.git@v0.8.0"
rb2engine convert /path/to/USB --repair-grid --grid-mode strict
rb2engine convert /path/to/USB --repair-grid --grid-mode majority
```

`--dry-run` never loads the model or analyzes audio. Default conversion does not
load the model either. Verification replays the recorded shifts; it does not
rerun inference. Source installs need the extra to verify a repaired library.

### What changes

An accepted correction adds the same sample offset to every source beat and/or
rotates the beat-within-bar labels. BPM, beat spacing, cues, loops, audio and
waveforms are retained. The original grid remains in Engine's default grid;
only the adjusted grid receives the correction. Reconvert with the switch off
to restore the source grid. Desktop conversion backs up an existing database
before rebuilding it. Do not rebuild after making Engine-only edits unless you
intend to replace them with the source export.

### Agreement policy

The detector uses BeatThis 1.1.0 small0, exported to ONNX, with NumPy
pre/postprocessing equivalent to upstream `dbn=False`. Its
[upstream API](https://github.com/CPJKU/beat_this#python-class) outputs both beats
and downbeats. License, checkpoint URL and file hashes travel with the model.
The maintainer export script is `packaging/export_grid_model.py`.

- Eligible tracks have a continuous 4/4, fixed-tempo source grid, at least 64
  beats, a sample rate of 44.1 or 48 kHz and a fitted tempo of 55–215 BPM.
  Nonlinear source timing or changing tempo/bar labels is skipped.
- Audio must be 90 seconds to 90 minutes. Three separated, non-overlapping
  passages of 30–45 seconds are decoded in the same sample clock as the
  overview generator; the 3-second context edges are excluded from evidence.
- Each usable passage needs at least 24 beats and 8 downbeats, at least 95%
  of predicted beats within 40 ms of the shifted source, at least 90%
  consecutive source-beat assignments and at least 90% support for one bar phase.
- Strict requires all three usable passages to agree on the bar phase and have
  inferred offsets within 20 ms. Relaxed requires any two, with identical
  per-passage checks. All three are preferred when they agree; otherwise the
  closest agreeing pair is used, with passage order as the tie-breaker.
- The time shift cannot exceed 200 ms or 40% of a beat, whichever is smaller.
  Shifts within 20 ms are left alone. Bar-phase correction can still apply.
  This is a small-offset repair, not correction of whole-beat timing errors.

Silence, weak intros/outros, ambiguous half/double tempo, insufficient evidence,
conflicting passages and unsupported grids keep the source grid. Decoder errors
are reported per track; missing/corrupt model components fail before publication.

### Evidence and verification

The JSON report records the selected mode, model/runtime identity, per-passage
quality measures, supporting passages, source/audio hashes and each outcome:
corrected, already aligned, skipped or failed. The publication journal binds the
repair record by SHA-256. Before publication the source export and analyzed
inputs are checked again. Verification checks both default and adjusted numbered
grids and validates the recorded source grid and audio for corrected tracks.

The NumPy/ONNX runtime matched the upstream PyTorch beat and downbeat arrays
exactly on 12 local 45-second passages from six tracks. These are implementation
parity checks, not an accuracy benchmark. On three-passage checks of those six
tracks, Strict retained five and judged one already aligned; Relaxed retained
two, judged three aligned and proposed an approximately +80.5 ms shift on one.
That proposal has not been independently accepted as musical ground truth.
A deliberately shifted variant of the aligned track was corrected by Strict
by approximately -74.5 ms and two bar beats, against the injected +80 ms/two-beat
error. The model's 20 ms frame resolution limits timing precision.

An authored percussion/bass fixture with injected +80 ms and two-beat phase
errors is also tested using the actual model, including the frozen desktop
applications. It yields an approximately -70 ms correction and restores the
bar phase. Deterministic policy tests cover both modes, conflicting/weak passages,
source preservation, restoration, record tampering and adjusted-grid mutation.

Model agreement establishes consistency, not musical truth. This remains an
experimental feature; listening and inspection in Engine DJ/on the target deck
are the separate acceptance step. No new variable-tempo analysis is enabled.
