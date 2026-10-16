# Build from the repository root: pyinstaller --noconfirm packaging/desktop.spec
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files

root = Path(SPECPATH).parent
runtime = root / 'build' / 'native-runtime'
native_binaries = [(str(p), 'rb2engine/native_runtime') for p in runtime.iterdir()
                   if p.is_file() and (p.suffix in ('.dll', '.dylib', '.exe') or p.name in ('ffmpeg', 'ffprobe', 'aac-reader'))]
if not native_binaries:
    raise RuntimeError('Build the waveform runtime first: bash packaging/build_native.sh')
analysis = Analysis(
    [str(root / 'packaging' / 'desktop_entry.py')],
    pathex=[str(root / 'src')],
    binaries=native_binaries,
    datas=collect_data_files('rb2engine', include_py_files=True),
    hiddenimports=['webview'],
    hookspath=[],
    excludes=['PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'tkinter', 'IPython', 'pytest', 'mutagen'],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(pyz, analysis.scripts, [], exclude_binaries=True, name='rb2engine',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False)
collection = COLLECT(exe, analysis.binaries, analysis.datas, strip=False, upx=False, name='rb2engine')
if sys.platform == 'darwin':
    app = BUNDLE(collection, name='rb2engine.app', bundle_identifier='io.github.shizuon.rb2engine',
                 info_plist={'NSHighResolutionCapable': True,
                             'NSRemovableVolumesUsageDescription': 'Read rekordbox exports and write Engine DJ libraries on your USB drive.',
                             'NSHumanReadableCopyright': 'MIT. See LICENSE and NOTICE.'})
