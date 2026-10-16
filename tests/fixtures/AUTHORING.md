# Tier-A fixture authoring checklist

Click-by-click instructions to produce **`tests/fixtures/mini_stick/`**: a
7-track rekordbox USB export of **self-authored** audio. This is the only
legal way to commit a real `export.pdb` + ANLZ tree — we create the music,
not shrink someone else's library.

**When:** before M3 (reader) and in parallel with M1 if possible.  
**Tools:** rekordbox (desktop), a USB stick, `ffmpeg` or `lame`, optional
`mutagen` / `eyeD3` for art verification.  
**Output:** copy the exported stick tree into `tests/fixtures/mini_stick/`.

> Do **not** write into a stick that holds irreplaceable library data. Use a
> blank/scratch USB, or a folder that you later treat as a drive root.

---

## 0. Prerequisites

| Item | Notes |
|---|---|
| rekordbox 6 or 7 free | Device export to USB must produce `PIONEER/rekordbox/export.pdb` |
| Scratch USB (FAT32) | Prefer empty; never use the user's 44 GB production stick for this |
| `ffmpeg` **or** `lame` | Maintainer-only; encodes MP3 once at authoring time |
| Optional: Python + mutagen | For post-export embedded-art hash verification |

---

## 1. Generate source audio

Create **seven** short tracks (≈30 s, 44.1 kHz mono, 64 kbps MP3). Distinct
pitches/tempos help you tell them apart in the GUI.

### 1.1 Recommended approach (`build_audio.py`)

When `tests/fixtures/build_audio.py` exists (planned maintainer script):

```bash
# From repo root; requires ffmpeg or lame on PATH
python tests/fixtures/build_audio.py --out /tmp/rb2engine_audio
```

Expected files:

```
t01_plain.mp3
t02_adjusted_grid.mp3
t03_tempo_change.mp3
t04_cues_full.mp3
t05_overflow.mp3
t06_loops.mp3
t07_loop_overflow.mp3
```

### 1.2 Manual fallback (no script yet)

```bash
mkdir -p /tmp/rb2engine_audio
# Example: 30 s sine at 440 Hz → MP3 (repeat with different freqs per track)
ffmpeg -f lavfi -i "sine=frequency=440:duration=30:sample_rate=44100" \
  -ac 1 -b:a 64k /tmp/rb2engine_audio/t01_plain.mp3
# t02: 480 Hz, t03: 520 Hz, … t07: 700 Hz — any distinct tones work
```

**Documented last-resort fallback:** 20 s mono 22.05 kHz WAV if MP3 encoding
is refused. That changes `sample_rate` in every fixture assertion — record it
in a note next to the fixture if you go this route.

### 1.3 Metadata and titles (forces string encoding)

Tag each file so rekordbox stores varied string lengths/encodings:

| File | Title | Artist | Album | Genre | BPM target | Notes |
|---|---|---|---|---|---|---|
| `t01_plain.mp3` | `t01_plain` | `RB2 Artist` | `RB2 Fixture` | `Test` | 128 | short ASCII |
| `t02_adjusted_grid.mp3` | `t02_adjusted_grid` | `RB2 Artist` | `RB2 Fixture` | `Test` | 128 | same album art as t01 |
| `t03_tempo_change.mp3` | `t03_tempo_change` | `RB2 Artist` | `RB2 Fixture` | `Test` | 120→140 | real tempo change later |
| `t04_cues_full.mp3` | `t04_cues_full` | `RB2 Artist` | `RB2 Fixture` | `Test` | 128 | 8 hot cues |
| `t05_overflow.mp3` | `t05_overflow` | `RB2 Artist` | `RB2 Fixture` | `Test` | 128 | **no album art** |
| `t06_loops.mp3` | `t06_loops` | `RB2 Artist` | `RB2 Fixture` | `Test` | 128 | loops + hot-cue-loop |
| `t07_loop_overflow.mp3` | `t07_loop_overflow` | `RB2 Artist` | `RB2 Fixture` | `Test` | 128 | 10 loops total |

**Non-ASCII coverage (required):** at least one of:

- A title longer than 127 ASCII chars **or** a Japanese/accented title on
  one track (exercises `device_sql_string` long-ascii `0x40` and/or
  utf16le `0x90`).
- Example title for t04: `t04_cues_full_日本語テスト` or  
  `t04_cues_full_áéíóú_ñ`.

Short ASCII titles on the others cover the packed short-ascii form.

You can set tags with:

```bash
# example with mutagen (install if needed: uv run python -c 'import mutagen')
# or use Music.app / Mp3tag / kid3
```

### 1.4 Album art assignment (U6)

Prepare **three** small PNG (or JPEG) cover images, e.g. 300×300 solid colors:

| Image | Color | Assigned to |
|---|---|---|
| `cover_shared.png` | e.g. blue | **both** `t01_plain` and `t02_adjusted_grid` (dedup case) |
| `cover_cues.png` | e.g. red | `t04_cues_full` only |
| `cover_loops.png` | e.g. green | `t06_loops` only |

- **`t05_overflow` must have no embedded art** (NULL path).
- `t03` and `t07` may be bare or reuse a cover; plan expects **exactly three**
  `AlbumArt` rows for seven tracks after conversion (`t01`/`t02` share one,
  plus `t04`, plus `t06`).

Embed with your tagger so the **same file bytes** go into t01 and t02.
Do not re-export art separately per track if your tool re-encodes.

---

## 2. Import into rekordbox

1. Open rekordbox → Collection.
2. File → Import → Import Track (or drag the seven MP3s in).
3. Confirm all seven appear with correct titles/artists.
4. Select all seven → right-click → **Analyze Track** (full analysis:
   BPM, beatgrid, waveforms). Wait until analysis completes.
5. Optional: set Key if analysis did not (any consistent keys are fine).

---

## 3. Per-track performance data (exact shapes)

Work in the track detail / player pane. Save cues after each track.

### 3.1 `t01_plain` — baseline (negative control)

- Leave the **auto-detected beatgrid untouched**.
- No hot cues, no memory cues, no loops.
- Confirm shared album art is visible in the browser.

### 3.2 `t02_adjusted_grid` — manual grid (criterion 4)

1. Open the beatgrid editor.
2. **Nudge the downbeat** so it is clearly different from the automatic
   detection (e.g. shift by ~1 beat, or re-set the first downbeat by ear).
3. Adjust BPM slightly if needed so the grid is visibly "edited".
4. Save. This is the positive control for `is_adjusted` / PQT2 presence.
5. Same cover as t01 must still be attached.

### 3.3 `t03_tempo_change` — real tempo change (R6)

1. Open the beatgrid / tempo editor.
2. Introduce a **real tempo change** mid-track (e.g. 120 BPM for the first
   half, 140 BPM for the second half), **or** use rekordbox's variable-tempo
   / multi-section grid tools so the grid is **not** constant-BPM.
3. Save. You need multiple tempo regions so multi-marker compression is
   exercised — a single constant BPM is not enough.

### 3.4 `t04_cues_full` — 8 hot cues, custom colors + names

Place **exactly 8 hot cues** on pads A–H (pads 1–8):

| Pad | Suggested position | Color | Name |
|---|---|---|---|
| A / 1 | ~0:02 | Red (custom) | `Intro` |
| B / 2 | ~0:05 | Orange | `Build` |
| C / 3 | ~0:08 | Yellow | `Drop` |
| D / 4 | ~0:12 | Green | `Break` |
| E / 5 | ~0:15 | Cyan | `Verse` |
| F / 6 | ~0:18 | Blue | `Chorus` |
| G / 7 | ~0:22 | Purple | `Outro` |
| H / 8 | ~0:26 | Pink/Magenta | **`キュー8`** or **`Cue_ñ`** (non-ASCII **required**) |

Rules:

- Each pad gets a **distinct custom color** (not all default).
- Each pad gets a **distinct name**.
- **At least one name is non-ASCII** (pad H above).
- These are hot cues, not memory cues.

### 3.5 `t05_overflow` — pad overflow (12 cues)

1. **4 hot cues** on pads A–D with any names/colors.
2. **8 memory cues** at distinct positions (Memory Cue mode / cue bank).
3. Total **12** cue-like points → Engine has only 8 pads; overflow is
   drop-and-itemize.
4. **No album art** on this track.

### 3.6 `t06_loops` — loops + hot-cue-that-is-a-loop (U3)

1. Create **3 saved loops** (named), e.g.:
   - Loop 1: 4 beats near intro — name `Loop_Intro`
   - Loop 2: 8 beats mid-track — name `Loop_Mid`
   - Loop 3: 4 beats near end — name `Loop_Out`
2. Create **one hot cue that is also a loop** (hot-cue-with-loop-out / loop
   hot cue — rekordbox: set a hot cue, then set loop in/out on that cue, or
   use "Hot Cue Loop" behavior depending on version).  
   **Policy under test:** this entry routes to a **loop slot** and **frees
   its pad** in Engine.
3. Distinct album art (`cover_loops.png`).

### 3.7 `t07_loop_overflow` — 10 loops (>8 loop slots)

1. **4 hot-cue loops** (each is a loop, not a plain cue) on distinct pads.
2. **6 saved memory loops** at other positions.
3. Total **10** loop-capable entries → Engine has 8 loop slots; 2 drop and
   must appear in the conversion report's `dropped_loops`.

---

## 4. Playlist tree (nested folders)

Create a folder hierarchy that is non-flat, e.g.:

```
RB2 Fixture/
├── Folder A/
│   ├── List Alpha     → t01, t02, t03
│   └── List Beta      → t04, t05
└── Folder B/
    └── List Gamma     → t06, t07
```

Requirements:

- At least **one nested folder** (folder containing a playlist or subfolder).
- **Stable track order** inside each playlist (note the order; golden IR
  will pin it).
- Playlist/folder **names** should include at least one short ASCII name;
  optional non-ASCII folder name is welcome.

---

## 5. Export to USB

1. Insert the **scratch** USB stick (FAT32).
2. In rekordbox: open the **Export** / device mode for that stick
   (rekordbox 6: bottom-left device panel; rekordbox 7: similar Device area).
3. Create a playlist collection on the device **or** drag the
   `RB2 Fixture` tree onto the device.
4. Ensure all **seven tracks** and the **playlist tree** are on the device
   collection.
5. Sync / export. Wait until the progress UI finishes with no errors.
6. Eject cleanly from the OS after rekordbox reports complete.

### Expected on-stick layout (names may vary slightly by version)

```
<USB root>/
  PIONEER/
    rekordbox/
      export.pdb
      exportExt.pdb          # may be present; tool warns, does not fail
    USBANLZ/
      ...                    # .DAT / .EXT / possibly .2EX per track
    Artwork/                 # often empty; art may be embedded only — OK
  Contents/
    ...                      # audio files under artist/title paths
```

---

## 6. Copy into the repo

```bash
# Mount path example (macOS) — use YOUR scratch stick mount, never production
SRC="/Volumes/SCRATCH_STICK"
DEST="/Users/jrgutier/src/rekordbox_to_engine/tests/fixtures/mini_stick"

rm -rf "$DEST"
mkdir -p "$DEST"
# Copy the export tree only — do not invent Engine Library/
cp -R "$SRC/PIONEER" "$DEST/"
cp -R "$SRC/Contents" "$DEST/"
```

Confirm:

```bash
ls -la "$DEST/PIONEER/rekordbox/export.pdb"
find "$DEST/PIONEER/USBANLZ" -type f | head
find "$DEST/Contents" -type f
```

---

## 7. Verification (do this before committing)

Do **not** commit a broken fixture. Check each item.

### 7.1 Structural

- [ ] `export.pdb` exists and is non-trivial size (typically tens–hundreds of KB for 7 tracks).
- [ ] ANLZ files exist under `USBANLZ` for all seven tracks (`.DAT` at minimum; `.EXT` expected for cues).
- [ ] All seven audio files exist under `Contents/` and open in a player.
- [ ] Playlist tree is present (will be confirmed after `rb2engine inspect` exists; until then, re-open the stick in rekordbox and confirm folders/lists).

### 7.2 Performance data (re-open stick in rekordbox)

| Check | Track | Pass? |
|---|---|---|
| Auto grid only, no manual edit | t01 | |
| Grid clearly adjusted | t02 | |
| Tempo changes mid-track | t03 | |
| 8 named hot cues, distinct colors, one non-ASCII name | t04 | |
| 4 hot + 8 memory = 12 | t05 | |
| 3 named loops + 1 hot-cue-loop | t06 | |
| 4 hot-cue loops + 6 saved loops = 10 | t07 | |
| Nested playlist folder tree | library | |
| Non-ASCII title and/or cue name present | t04 / titles | |

### 7.3 Album art byte-identity (dedup) — **critical**

After export, extract the **embedded** picture from **t01** and **t02**
(from the files under `Contents/`, not from `PIONEER/Artwork/`):

```bash
# Example with mutagen (adjust paths to the real Contents/ files)
uv run python - <<'PY'
from pathlib import Path
import hashlib
from mutagen.id3 import ID3

def pic_bytes(path: Path) -> bytes:
    tags = ID3(path)
    apics = tags.getall("APIC")
    assert apics, f"no APIC in {path}"
    return apics[0].data

# Set these to the two exported file paths
p1 = Path("tests/fixtures/mini_stick/Contents/.../t01_plain.mp3")
p2 = Path("tests/fixtures/mini_stick/Contents/.../t02_adjusted_grid.mp3")
b1, b2 = pic_bytes(p1), pic_bytes(p2)
assert b1 == b2, "t01/t02 art not byte-identical — fix tags before golden IR"
h = hashlib.sha1(b1).hexdigest()  # 40-char lowercase hex from hashlib
print("sha1 full (40):", h)
print("our key (strip leading zero digits):", h.lstrip("0") or "0")
PY
```

Or with `sha1sum` on extracted image files:

```bash
# After dumping the image bytes to /tmp/t01_art.bin and /tmp/t02_art.bin
sha1sum /tmp/t01_art.bin /tmp/t02_art.bin
# Digests must match. Then strip leading zeros for the golden key:
python3 -c "print(open('/tmp/t01_art.bin','rb').read())"  # better:
python3 - <<'PY'
import hashlib, pathlib
b = pathlib.Path("/tmp/t01_art.bin").read_bytes()
h = hashlib.sha1(b).hexdigest()
print("sha1sum-style (zero-padded 40):", h)
print("rb2engine key (leading zero digits stripped):", h.lstrip("0") or "0")
PY
```

#### Hash rules (read carefully)

| Rule | Detail |
|---|---|
| Algorithm | **`sha1` of the raw image bytes** |
| Case | **lowercase** hex |
| Padding | **Leading zero digits stripped** (Engine-shaped unpadded key) |

**Warnings:**

1. **`sha1sum` zero-pads to 40 hex characters.** Our dedup key does **not**.
   If the digest starts with `0`, recording the unstripped `sha1sum` output
   will **silently disagree** with the mapper (~6% of images). Always strip
   leading `0` digits (if the entire digest were zeros, keep a single `0`).
2. **Do not paste the tool's own output** (`rb2engine inspect --mapped` or
   mapper logs) into `golden_mapped_ir.json` as the expected hash. That
   produces a transcript test: the fixture would only prove the code matches
   itself. Compute hashes **independently** with `sha1sum` / `hashlib` from
   the image bytes **before** the mapper exists (or without using its output).
3. If t01 and t02 digests **differ**, re-tag both from the **same** image file
   and re-export. Do **not** weaken the "one shared `AlbumArt` row" assertion.

Also verify:

- [ ] t04 and t06 have **distinct** art hashes from each other and from the shared cover.
- [ ] t05 has **no** embedded art.

### 7.4 Record expected hashes (for later golden IR)

Write them down (or into a local note — not by running unfinished code):

```
shared (t01+t02): <sha1 stripped>
t04:              <sha1 stripped>
t06:              <sha1 stripped>
```

### 7.5 After the reader lands (M3)

```bash
uv run rb2engine inspect tests/fixtures/mini_stick --json \
  > tests/fixtures/golden_ir.json
```

Until then, the structural + rekordbox re-open + art hash checks are the gate.

---

## 8. Commit hygiene

- Commit `tests/fixtures/mini_stick/` binaries only after §7 passes.
- Do **not** commit real user sticks or third-party commercial audio.
- Do **not** overwrite `tests/fixtures/golden/engine_ref.db` or other
  Engine-authored golden artifacts as part of Tier-A authoring (those are
  Tier C / M1).

---

## Track purpose quick reference

| Track | Purpose |
|---|---|
| `t01_plain` | Baseline metadata; **no** grid adjustment (`is_adjusted` negative control); shared art |
| `t02_adjusted_grid` | Manually adjusted beatgrid (criterion 4); shared art with t01 |
| `t03_tempo_change` | Real tempo change → multi-marker grid compression (R6) |
| `t04_cues_full` | 8 hot cues, distinct custom colors/names, one non-ASCII name |
| `t05_overflow` | 4 hot + 8 memory = 12 → pad overflow; **no art** |
| `t06_loops` | 3 saved loops + 1 hot-cue-that-is-a-loop (U3 pad free) |
| `t07_loop_overflow` | 4 hot-cue loops + 6 saved loops = 10 → loop-slot overflow |

## Bundled audio runtime fixture

`audio/authored-440hz.mp3` is an original synthetic 440 Hz sine wave, three seconds,
44.1 kHz stereo, encoded at 96 kbit/s using FFmpeg's libmp3lame encoder. No recorded
music is included. It is MIT-licensed with this repository. Reproduce with:

```sh
ffmpeg -v error -f lavfi -i sine=frequency=440:sample_rate=44100:duration=3 -ac 2 -c:a libmp3lame -b:a 96k -map_metadata -1 authored-440hz.mp3
```

The decoder tests assert the known frequency, three-second clock and unchanged
source bytes; they do not use this encoded fixture as an Engine-native reference.
