# Desktop app

The desktop app provides folder selection, library inspection, conversion and
verification in one small window. Chinese and English are available. Source
exports and audio are preserved. It uses the existing CLI conversion engine.

## Windows x64

Download `rb2engine-<version>-windows-x64.zip` from the **Desktop builds** workflow
artifact (or a release, when available). Extract the entire ZIP and run
`rb2engine.exe`. Keep `_internal` alongside the executable. Python is bundled.

Windows 10/11 x64 and the **Microsoft Edge WebView2 Evergreen Runtime** are
required. If the runtime is missing, the app shows a startup error with the log
location. Run the included, Microsoft-signed `MicrosoftEdgeWebview2Setup.exe`;
this bootstrapper downloads the runtime and needs internet access. An offline
installer is available from [Microsoft](https://developer.microsoft.com/microsoft-edge/webview2/).
The app deliberately requires WebView2; it does not fall back to Internet Explorer.

Select the drive root, such as `E:\`, containing `PIONEER`. A USB SSD may appear
as a fixed disk; both fixed and removable local drive letters are enumerated.
Network and optical drives are not probed automatically. The folder chooser
and manual path field remain available. Press Enter or the refresh button to
read a manually entered path. After choosing a folder, inspection starts automatically.

Logs: `%LOCALAPPDATA%\rb2engine\desktop.log`.

These builds are not Authenticode signed. SmartScreen may identify an unknown
publisher. Windows CI runs the actual frozen executable using WebView2 and a
small authored export under a path containing Chinese, spaces, `#` and `%`.
This checks startup, the JavaScript bridge, UI buttons, conversion and verification;
it does not certify every Windows installation, physical USB drive or DJ player.
Windows ARM64 and 32-bit Windows have no desktop builds here.

## macOS Apple Silicon

Extract `rb2engine-<version>-macos-arm64.zip` and open `rb2engine.app`.
The app is ad-hoc signed for local execution, not Developer ID signed or notarized.
macOS may require the normal Privacy & Security approval for an unnotarized app.
No security setting needs to be disabled.

Logs: `~/Library/Logs/rb2engine/desktop.log`.

## Conversion and recovery

Finish exporting, then close rekordbox and Engine DJ. Choose the USB and click
**Convert**. Confirm that DJ software is closed before conversion or waveform publication.
The export fingerprint is rechecked before writing. An existing Engine `m.db`
is snapshotted with SQLite backup under the writer lock before rebuilding:

```
Engine Library/Database2/rb2engine-backups/m-<unique-id>.db
```

These are database recovery copies, not full backups of audio or the entire
Engine library. Backup failure aborts the rebuild. The usual atomic database
replacement and cross-process library lock remain in effect. The window cannot
close while an operation is running. Safely eject the drive only after completion.

Use **Verify** after conversion. Skips, dropped cues/loops and source warnings
are available in the collapsed **Report**. The JSON report is written under
`Engine Library/rb2engine-report.json`. Verification is a mechanical comparison;
check actual playback on your equipment before a show.

Enable **Fill Engine overview waveforms** to generate 1,024-point three-band and
RGB whole-track overviews after conversion. The Windows x64 and macOS ARM64
packages include FFmpeg, the AAC reader and the waveform processor; no compiler,
Python, FFmpeg install or internet connection is needed for this feature.
This does not create the scrolling high-resolution waveform.

For an already converted drive, **Fill waveforms** adds missing overviews without
rebuilding the library. Existing waveforms are preserved, including tracks with
only one of the two overview formats. An explicitly confirmed full reconversion
regenerates RGB files left by the previous database and saves their old bytes in
the waveform backup. This operation requires Engine schema 3.0.2;
conversion with the option enabled creates that schema. Unsupported audio or a
generation failure leaves the waveform batch unpublished; successfully processed
files remain cached for retry. The base conversion remains saved if a later
waveform step fails, and **Report** identifies the failure.

Before publication, the complete database state and audio hashes are checked.
Metadata, keys, grids, cues, loops, playlists and existing waveform bytes are
protected; only empty waveform payloads and the matching timing header are
updated. Backups, the publication plan and recovery journal are retained under
`Engine Library/rb2engine-waveforms/<batch>/`. The report contains that path.
The existing CLI `rb2engine waveforms restore <batch>` can restore a batch.
Publication verifies three-band/RGB readback and source hashes automatically.
Do not eject the drive or start Engine DJ until processing finishes.

The included FFmpeg build disables GPL/nonfree components and networking. Its
LGPL license, complete corresponding source tarball, build configuration and
build script are included under `licenses/FFmpeg`. Its dynamic libraries remain
replaceable; this project's MIT license does not replace FFmpeg's license.

## Fill missing keys with OpenKeyScan

Enable **Fill missing keys with OpenKeyScan** to analyze only tracks with an empty
rekordbox key. Existing keys are preserved. The result is written to the Engine
library and conversion report; audio tags and the rekordbox export are unchanged.

Install [OpenKeyScan](https://www.openkeyscan.com/download) separately. Its analyzer
is not bundled with rb2engine. Standard Windows and macOS installations are
detected automatically, including Windows MSIX packages and installer registrations. When detected,
no download or installation prompt is shown. If the option is enabled and the
analyzer cannot be found, **Download** and **Locate installation** appear. Select
`OpenKeyScan.exe`, `OpenKeyScan.app`, or `openkeyscan-analyzer.exe`; a manually
selected location is remembered for the next launch.

Analysis runs locally through the installed standalone analyzer. Individual
decoding failures leave the key empty and are listed in **Report**. Startup or
protocol failures stop conversion before publishing a database. The report keeps
the prediction and audio hash, paired with the publication journal, so **Verify**
can check the enriched library without rerunning the model. A successful check
confirms the stored result, not musical accuracy; predictions can be wrong.

## Build and test

```
uv sync --extra desktop --dev
uv run rb2engine-gui
uv pip install pyinstaller==6.22.3
bash packaging/build_native.sh
uv run pyinstaller --noconfirm packaging/desktop.spec
uv run python packaging/archive_desktop.py windows-x64
```

Use `macos-arm64` for the archive label when building on Apple Silicon.
Build on the target operating system. On Windows, run the native build in an
MSYS2 UCRT64 environment with GCC, make, pkgconf and nasm installed. The workflow packages the result and
uploads its SHA-256 file plus separate native-renderer smoke evidence.
The desktop dependency is optional; ordinary CLI installs do not require it.
