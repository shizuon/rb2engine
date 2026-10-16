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
**Convert**. The confirmation is shown only when starting a conversion.
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

Waveform generation remains in the CLI and is not exposed by this GUI. The
existing Windows waveform-generation limitation is unchanged.

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
uv run pyinstaller --noconfirm packaging/desktop.spec
uv run python packaging/archive_desktop.py windows-x64
```

Use `macos-arm64` for the archive label when building on Apple Silicon.
Build on the target operating system. The workflow packages the result and
uploads its SHA-256 file plus separate native-renderer smoke evidence.
The desktop dependency is optional; ordinary CLI installs do not require it.
