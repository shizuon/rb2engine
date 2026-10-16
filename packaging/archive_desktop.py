"""Archive the desktop bundle with Python dependency licenses and checksums."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import shutil
import subprocess
import sys
from pathlib import Path

label = sys.argv[1]
root = Path(__file__).resolve().parent.parent
out = root / 'desktop-dist'
out.mkdir(exist_ok=True)
version = importlib.metadata.version('rb2engine')
name = f'rb2engine-{version}-{label}'
bundle = root / 'dist' / ('rb2engine.app' if sys.platform == 'darwin' else 'rb2engine')
licenses = bundle / ('Contents/Resources/licenses' if sys.platform == 'darwin' else 'licenses')
licenses.mkdir(parents=True, exist_ok=True)
for source in ('LICENSE', 'NOTICE'):
    shutil.copy2(root / source, licenses / source)
ffmpeg_license = licenses / 'FFmpeg'
ffmpeg_license.mkdir(exist_ok=True)
for source in ('COPYING.LGPLv2.1', 'LICENSE.md', 'config.h', 'config.mak'):
    shutil.copy2(root / 'build/native-runtime' / source, ffmpeg_license / source)
# Supply complete corresponding LGPL source and exact configuration/build script.
# Libraries remain dynamically linked and replaceable in native_runtime.
shutil.copy2(root / 'build/ffmpeg-8.0.3.tar.xz', ffmpeg_license)
shutil.copy2(root / 'packaging/build_native.sh', ffmpeg_license)
for source in ('dsp.c', 'aac_decode.c'):
    shutil.copy2(root / 'src/rb2engine/overviews' / source, ffmpeg_license)
if sys.platform == 'win32':
    for package in ('gcc-libs', 'winpthreads'):
        source = Path('C:/msys64/ucrt64/share/licenses') / package
        if source.is_dir():
            shutil.copytree(source, licenses / package, dirs_exist_ok=True)
# Retain the license texts installed with bundled Python dependencies. Including
# a few build-only distributions is harmless and avoids losing transitive notices.
for distribution in importlib.metadata.distributions():
    for file in distribution.files or []:
        if any(word in str(file).lower() for word in ('license', 'copying', 'notice')):
            source = Path(distribution.locate_file(file))
            if not source.is_file() or source.stat().st_size > 2_000_000:
                continue
            destination = licenses / distribution.metadata['Name'] / str(file).replace('/', '_')
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
archive = out / f'{name}.zip'
if sys.platform == 'darwin':
    # Preserve .app symlinks and file modes. Re-sign after adding license files.
    subprocess.run(['codesign', '--force', '--deep', '--sign', '-', str(bundle)], check=True)
    subprocess.run(['ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', str(bundle), str(archive)], check=True)
else:
    shutil.make_archive(str(out / name), 'zip', root_dir=bundle.parent, base_dir=bundle.name)
digest = hashlib.sha256(archive.read_bytes()).hexdigest()
(out / f'{name}.sha256').write_text(f'{digest}  {archive.name}\n', encoding='utf-8')
(out / f'{name}.json').write_text(json.dumps({
    'version': version, 'platform': label, 'sha256': digest,
    'signed_for_distribution': False, 'smoke_test': 'see smoke artifact for runtime evidence',
}, indent=2), encoding='utf-8')
print(archive)
