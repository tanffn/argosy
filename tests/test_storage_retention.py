import gzip
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from argosy.services.backup_storage import compress_snapshot, restore_snapshot, snapshot
from argosy.services.transcript_archive import archive_transcripts, read_bundle_file

NOW = datetime(2026, 9, 23, tzinfo=UTC)


def database(path):
    with sqlite3.connect(path) as conn:
        conn.execute("create table evidence (v text)")
        conn.execute("insert into evidence values ('preserved')")


def test_compressed_online_backup_restores_and_never_overwrites_restore(tmp_path):
    source = tmp_path / "source.db"
    database(source)
    artifact = tmp_path / "backup.db.gz"
    result = snapshot(source, artifact)
    assert result['quick_check'] == 'ok'
    restored = tmp_path / "restored.db"
    restore_snapshot(artifact, restored)
    with sqlite3.connect(restored) as conn:
        assert conn.execute('select * from evidence').fetchall() == [('preserved',)]
    with pytest.raises(FileExistsError):
        restore_snapshot(artifact, restored)


def test_old_snapshot_compression_is_byte_exact_and_preserves_original(tmp_path):
    source = tmp_path / 'argosy-20260901.db'
    database(source)
    original = source.read_bytes()
    result = compress_snapshot(source)
    assert gzip.decompress(Path(result['path']).read_bytes()) == original
    assert source.read_bytes() == original
    with pytest.raises(FileExistsError):
        compress_snapshot(source)


def test_bad_gzip_does_not_publish_restoration(tmp_path):
    source = tmp_path / 'bad.gz'
    source.write_bytes(gzip.compress(b'not sqlite'))
    target = tmp_path / 'restored.db'
    with pytest.raises(sqlite3.DatabaseError):
        restore_snapshot(source, target)
    assert not target.exists()


def transcript(root, day, *, modified_days_ago=60, body=b'evidence'):
    path = root / 'a' / day / '123__phase' / 'transcript.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    timestamp = (NOW - timedelta(days=modified_days_ago)).timestamp()
    os.utime(path, (timestamp, timestamp))
    return path


def test_archive_preserves_history_and_keeps_recent_or_recently_written_files(tmp_path):
    old = transcript(tmp_path, '2026-07-01')
    recent = transcript(tmp_path, '2026-09-20')
    updated = transcript(tmp_path, '2026-07-02', modified_days_ago=1)
    result = archive_transcripts(tmp_path, now=NOW)
    assert result['archived_files'] == 1
    assert not old.exists()
    assert read_bundle_file(tmp_path, old) == b'evidence'
    assert recent.exists() and updated.exists()
    assert archive_transcripts(tmp_path, now=NOW)['status'] == 'not_due'


def test_archive_retry_merges_week_without_losing_older_members(tmp_path):
    first = transcript(tmp_path, '2026-07-01', body=b'first')
    archive_transcripts(tmp_path, now=NOW)
    second = transcript(tmp_path, '2026-07-02', body=b'second')
    archive_transcripts(tmp_path, now=NOW, force=True)
    assert read_bundle_file(tmp_path, first) == b'first'
    assert read_bundle_file(tmp_path, second) == b'second'
    assert len(list((tmp_path / '_archive/a').glob('*.zip'))) == 1


def test_archive_conflict_retains_raw_file_and_prior_zip(tmp_path):
    old = transcript(tmp_path, '2026-07-01')
    archive_transcripts(tmp_path, now=NOW)
    archive = next((tmp_path / '_archive/a').glob('*.zip'))
    before = archive.read_bytes()
    transcript(tmp_path, '2026-07-01', body=b'changed')
    with pytest.raises(RuntimeError, match='conflict'):
        archive_transcripts(tmp_path, now=NOW, force=True)
    assert old.read_bytes() == b'changed'
    assert archive.read_bytes() == before


def test_archive_publish_failure_never_removes_source(tmp_path, monkeypatch):
    old = transcript(tmp_path, '2026-07-01')
    def fail(*args):
        raise OSError('disk failure')
    monkeypatch.setattr('argosy.services.transcript_archive.durable_replace', fail)
    with pytest.raises(OSError, match='disk failure'):
        archive_transcripts(tmp_path, now=NOW)
    assert old.exists()


def test_archive_lookup_rejects_traversal(tmp_path):
    root = tmp_path / 'transcripts'
    root.mkdir()
    outside = tmp_path / 'private.md'
    outside.write_text('private')
    with pytest.raises(ValueError, match='escapes'):
        read_bundle_file(root, root / '..' / 'private.md')


def test_writers_and_archivers_share_lock(tmp_path):
    from argosy.services.transcript_archive import transcript_lock
    with transcript_lock(tmp_path):
        with pytest.raises(TimeoutError):
            with transcript_lock(tmp_path, timeout_s=0.01):
                pytest.fail('Second writer unexpectedly acquired transcript lock')


def test_backup_retention_ignores_similar_manual_names(tmp_path):
    from argosy.agent_settings import AgentSettings, BackupsBlock
    from argosy.orchestrator.loops.backup import BackupLoop
    from argosy.orchestrator.loops.base import LoopSchedule
    manual = tmp_path / 'argosy-manual-argosy-20260803.db'
    manual.write_bytes(b'keep')
    for name in ['argosy-20260920.db', 'argosy-20260920.db.gz', 'argosy-20260919.db.gz', 'argosy-20260918.db.gz']:
        (tmp_path / name).touch()
    loop = BackupLoop(schedule=LoopSchedule(cron='0 3 * * *'),
        settings=AgentSettings(backups=BackupsBlock(retention_daily=2,retention_weekly=0,retention_monthly=0)))
    loop._enforce_retention(tmp_path, moment=NOW)
    assert manual.exists()
    assert (tmp_path / 'argosy-20260919.db.gz').exists()
    assert not (tmp_path / 'argosy-20260918.db.gz').exists()


def test_locked_transcript_backup_includes_archived_and_recent(tmp_path):
    from scripts.copy_transcripts import copy_transcripts
    source = tmp_path / 'source'
    old = transcript(source, '2026-07-01')
    recent = transcript(source, '2026-09-20')
    archive_transcripts(source, now=NOW)
    target = tmp_path / 'copy'
    assert copy_transcripts(source, target) == 3  # archive + stamp + recent
    assert read_bundle_file(target, target / old.relative_to(source)) == b'evidence'
    assert read_bundle_file(target, target / recent.relative_to(source)) == b'evidence'


def test_writer_rejects_escape_before_creating_directories(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from argosy.services import transcript_writer
    monkeypatch.setattr(transcript_writer, 'get_settings', lambda: SimpleNamespace(home=tmp_path))
    with pytest.raises(ValueError, match='escapes'):
        transcript_writer.write_phase_bundle(user_id='../outside', decision_run_id=1,
            phase_kind='test', started_at=NOW, finished_at=NOW, verdict=None, participants=[])
    assert not (tmp_path / 'outside').exists()


@pytest.mark.skipif(os.name != 'nt', reason='Windows junction safety')
def test_snapshot_rejects_junction_in_destination_ancestry(tmp_path):
    import subprocess
    source = tmp_path / 'source.db'
    database(source)
    external = tmp_path / 'external'
    external.mkdir()
    link = tmp_path / 'link'
    subprocess.run(['powershell.exe', '-NoProfile', '-Command',
        f"New-Item -ItemType Junction -Path '{link}' -Target '{external}'"],
        check=True, capture_output=True)
    with pytest.raises(ValueError, match='link/reparse'):
        snapshot(source, link / 'new' / 'backup.db.gz')
    from argosy.agent_settings import AgentSettings, BackupsBlock
    from argosy.orchestrator.loops.backup import BackupLoop
    from argosy.orchestrator.loops.base import LoopSchedule
    loop = BackupLoop(schedule=LoopSchedule(cron='0 3 * * *'),
        settings=AgentSettings(backups=BackupsBlock(backups_dir=str(link))))
    with pytest.raises(ValueError, match='link/reparse'):
        loop._resolve_paths()
    with pytest.raises(ValueError, match='link/reparse'):
        loop._enforce_retention(link, moment=NOW)
    from argosy.services.backup_storage import copy_artifact
    with pytest.raises(ValueError, match='link/reparse'):
        copy_artifact(source, link / 'backup.db.gz')
    assert not list(external.iterdir())


def test_offsite_publish_failure_preserves_previous_artifact(tmp_path, monkeypatch):
    from argosy.services.backup_storage import copy_artifact
    source, target = tmp_path / 'source.gz', tmp_path / 'target.gz'
    source.write_bytes(b'new snapshot')
    target.write_bytes(b'previous snapshot')
    def fail(*args):
        raise OSError('unavailable disk')
    monkeypatch.setattr('argosy.services.backup_storage.durable_replace', fail)
    with pytest.raises(OSError, match='unavailable disk'):
        copy_artifact(source, target)
    assert target.read_bytes() == b'previous snapshot'
    assert not list(tmp_path.glob('argosy-snapshot-*'))


@pytest.mark.skipif(os.name != 'nt', reason='Windows cleanup helper')
@pytest.mark.parametrize('case', ['preview', 'mismatch', 'tracked', 'wal'])
def test_reclaim_preview_checks_exact_compressed_copy(tmp_path, case):
    import shutil
    import subprocess
    repo = tmp_path / 'repo'
    (repo / 'scripts').mkdir(parents=True)
    (repo / 'backups').mkdir()
    subprocess.run(['git', 'init', str(repo)], check=True, capture_output=True)
    script = repo / 'scripts/reclaim_storage.ps1'
    shutil.copyfile(Path(__file__).resolve().parents[1] / 'scripts/reclaim_storage.ps1', script)
    raw = repo / 'backups/argosy-20260901.db'
    raw.write_bytes(b'evidence')
    compressed = raw.with_suffix('.db.gz')
    compressed.write_bytes(gzip.compress(b'different' if case == 'mismatch' else b'evidence'))
    if case == 'tracked':
        subprocess.run(['git', '-C', str(repo), 'add', str(raw)], check=True, capture_output=True)
    if case == 'wal':
        Path(str(raw) + '-wal').write_bytes(b'pending transactions')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-File', str(script)],
                            capture_output=True, text=True)
    assert (result.returncode == 0) == (case == 'preview'), result.stderr
    assert raw.read_bytes() == b'evidence'
    assert compressed.exists()
