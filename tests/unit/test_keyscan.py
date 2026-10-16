"""Protocol, installed-app discovery, selective enrichment and verification integrity."""
from __future__ import annotations

import dataclasses
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from rb2engine import keyscan
from rb2engine.desktop.service import DesktopService
from rb2engine.errors import FatalError
from rb2engine.reader.library import read_library
from rb2engine.report import REPORT_FILENAME
from rb2engine.verify import verify_library
from tests.desktop_fixture import make_drive
from tests.unit.test_desktop import manifest, wait


@pytest.fixture
def fake_analyzer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    executable = tmp_path / 'openkeyscan-analyzer.exe'
    executable.write_text('''import json, sys
print(json.dumps({"type":"ready"}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    if not __import__('pathlib').Path(request['path']).is_file():
        result = {'status':'error','error':'Audio is missing'}
    else:
        result = {'status':'success','openkey':'1m','camelot':'8A'}
    print(json.dumps({'id':request['id'], **result}), flush=True)
''', encoding='utf-8')
    real_analyzer = keyscan.Analyzer
    monkeypatch.setattr(keyscan, 'Analyzer', lambda _command: real_analyzer(
        [sys.executable, str(executable)], timeout=5,
    ))
    monkeypatch.setattr(keyscan, 'config_path', lambda: tmp_path / 'settings.json')
    monkeypatch.setattr(keyscan, 'find_installed_analyzer', lambda: str(executable))
    return executable


def test_only_empty_keys_are_filled_and_audio_is_unchanged(
    tmp_path: Path, fake_analyzer: Path,
) -> None:
    root = make_drive(tmp_path / '演出 USB #1 100%')
    library = read_library(root)
    original = manifest(root)
    track = library.tracks[1]
    library = dataclasses.replace(library, tracks={
        1: track, 2: dataclasses.replace(track, rb_id=2, key_name='8B'),
        3: dataclasses.replace(track, rb_id=3, key_name='  '),
        4: dataclasses.replace(track, rb_id=4, resolved_path=None),
    })
    progress = []
    enriched, record = keyscan.fill_missing_keys(
        library, fake_analyzer, on_progress=lambda *args: progress.append(args),
    )
    assert enriched.tracks[1].key_name == '1m'
    assert enriched.tracks[2] is library.tracks[2]
    assert enriched.tracks[3].key_name == '1m'
    assert enriched.tracks[4].key_name is None
    assert library.tracks[1].key_name is None
    assert [r['track_id'] for r in record['tracks']] == [1, 3]
    assert [r['track_id'] for r in record['failures']] == [4]
    assert progress[-1] == ('analyzing keys', 3, 3)
    assert manifest(root) == original
    assert keyscan.replay_keys(library, record) == enriched
    assert keyscan.fill_missing_keys(
        dataclasses.replace(library, tracks={2: library.tracks[2]}), fake_analyzer,
    )[1]['tracks'] == []


def test_enriched_database_verifies_but_mutation_does_not(
    tmp_path: Path, fake_analyzer: Path,
) -> None:
    root = make_drive(tmp_path / 'USB #2 % 演出')
    original = manifest(root)
    service = DesktopService()
    assert service.get_keyscan()['path'] == str(fake_analyzer)
    service.inspect_drive(str(root))
    assert wait(service)['status'] == 'ready'
    service.convert_drive(False, True, False, True)
    state = wait(service)
    assert state['status'] in ('complete', 'warning'), state
    assert '1 missing keys filled' in state['details']
    assert verify_library(root).exit_code == 0
    database = root / 'Engine Library/Database2/m.db'
    with sqlite3.connect(database) as conn:
        assert conn.execute('SELECT key FROM Track').fetchone() == (1,)
        conn.execute('UPDATE Track SET key=4')
    assert verify_library(root).exit_code == 1
    assert manifest(root) == original
    report = root / 'Engine Library' / REPORT_FILENAME
    data = json.loads(report.read_text(encoding='utf-8'))
    data['key_analysis']['tracks'][0]['key'] = '3m'
    report.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(FatalError, match='missing or changed'):
        verify_library(root)


def test_missing_analyzer_fails_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(keyscan, 'saved_analyzer', lambda: '')
    monkeypatch.setattr(keyscan, 'find_installed_analyzer', lambda: '')
    root = make_drive(tmp_path / 'USB')
    service = DesktopService()
    service.inspect_drive(str(root))
    wait(service)
    service.convert_drive(False, True, False, True)
    state = wait(service)
    assert state['status'] == 'error'
    assert keyscan.DOWNLOAD_URL in state['error']
    assert not (root / 'Engine Library').exists()


@pytest.mark.parametrize('layout', [
    'OpenKeyScan.app/Contents/Resources/app.asar.unpacked/lib/openkeyscan-analyzer/'
    'openkeyscan-analyzer',
    'OpenKeyScan/resources/app.asar.unpacked/lib/openkeyscan-analyzer/openkeyscan-analyzer.exe',
])
def test_actual_electron_layout_detection_and_saved_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str,
) -> None:
    binary = tmp_path / layout
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b'fixture')
    selected = tmp_path / layout.split('/', maxsplit=1)[0]
    assert keyscan.resolve_analyzer(str(selected)) == binary.resolve()
    monkeypatch.setattr(keyscan, 'config_path', lambda: tmp_path / 'config/settings.json')
    assert keyscan.saved_analyzer() == ''
    keyscan.save_analyzer(str(binary))
    assert keyscan.saved_analyzer() == str(binary.resolve())
    binary.unlink()
    assert keyscan.saved_analyzer() == ''
    with pytest.raises(FatalError, match='not found'):
        keyscan.resolve_analyzer(str(selected))


@pytest.mark.parametrize(('reply', 'error'), [
    ({'id': 'wrong', 'status': 'success', 'openkey': '1m'}, FatalError),
    ({'id': '1', 'status': 'error', 'error': 'Decode failed'}, ValueError),
    ({'id': '1', 'status': 'success', 'openkey': '13m'}, ValueError),
    ({'id': '1', 'status': 'success', 'openkey': '1m', 'camelot': '8B'}, ValueError),
])
def test_invalid_analysis_responses(tmp_path: Path, reply: dict, error: type[Exception]) -> None:
    script = tmp_path / 'analyzer.py'
    script.write_text(
        "import json,sys\nprint('{\"type\":\"ready\"}',flush=True)\n"
        f"for line in sys.stdin: print({json.dumps(reply)!r},flush=True)\n", encoding='utf-8',
    )
    analyzer = keyscan.Analyzer([sys.executable, str(script)], timeout=2)
    try:
        with pytest.raises(error):
            analyzer.analyze(tmp_path / '音乐.wav', '1')
    finally:
        analyzer.close()


@pytest.mark.parametrize('program', [
    "print('bad JSON',flush=True)",
    "print('{}',flush=True)",
    "import time; time.sleep(20)",
    "import sys; print('failed loading model',file=sys.stderr)",
])
def test_startup_failures_are_bounded_and_process_is_closed(tmp_path: Path, program: str) -> None:
    script = tmp_path / 'analyzer.py'
    script.write_text(program, encoding='utf-8')
    with pytest.raises(FatalError, match='could not start'):
        keyscan.Analyzer([sys.executable, str(script)], timeout=0.5)


def test_replay_rejects_changed_audio_or_existing_keys(
    tmp_path: Path, fake_analyzer: Path,
) -> None:
    root = make_drive(tmp_path / 'USB')
    library = read_library(root)
    enriched, record = keyscan.fill_missing_keys(library, fake_analyzer)
    with pytest.raises(FatalError, match='existing key'):
        keyscan.replay_keys(enriched, record)
    (root / 'Contents/test.wav').write_bytes(b'changed')
    with pytest.raises(FatalError, match='changed or is missing'):
        keyscan.replay_keys(library, record)
