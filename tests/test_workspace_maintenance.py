"""Recovery tooling must preserve live data and take consistent WAL snapshots."""
import sqlite3
import subprocess
from pathlib import Path

import pytest

from scripts.backup_sqlite import snapshot


def test_online_snapshot_includes_committed_wal_not_uncommitted_rows(tmp_path):
    source = tmp_path / "live.db"
    target = tmp_path / "backup.db"
    live = sqlite3.connect(source)
    try:
        live.execute("pragma journal_mode=wal")
        live.execute("create table evidence (value text)")
        live.execute("insert into evidence values ('committed')")
        live.commit()
        live.execute("insert into evidence values ('not committed')")
        snapshot(source, target)
        with sqlite3.connect(target) as restored:
            assert restored.execute("select * from evidence").fetchall() == [("committed",)]
            assert restored.execute("pragma quick_check").fetchone() == ("ok",)
        assert live.in_transaction
    finally:
        live.close()


def test_snapshot_rejects_live_target_and_existing_sidecars(tmp_path):
    source = tmp_path / "live.db"
    with sqlite3.connect(source) as conn:
        conn.execute("create table evidence (value text)")
    with pytest.raises(ValueError, match="differ"):
        snapshot(source, source)
    destination = tmp_path / "backup.db"
    destination.write_bytes(b"prior backup")
    Path(str(destination) + "-wal").touch()
    with pytest.raises(ValueError, match="sidecars"):
        snapshot(source, destination)
    assert destination.read_bytes() == b"prior backup"


def test_bad_database_preserves_previous_backup(tmp_path):
    source = tmp_path / "bad.db"
    source.write_bytes(b"not a database")
    destination = tmp_path / "backup.db"
    destination.write_bytes(b"prior backup")
    with pytest.raises(sqlite3.DatabaseError):
        snapshot(source, destination)
    assert destination.read_bytes() == b"prior backup"
    assert not list(tmp_path.glob("argosy-snapshot-*"))


@pytest.mark.skipif(__import__('sys').platform != 'win32', reason="Windows maintenance script")
@pytest.mark.parametrize('filename', ['proof.txt', '\u00e9.txt', '\u05e9\u05dc\u05d5\u05dd.txt'])
def test_cleanup_preview_and_tracked_file_protection(tmp_path, filename):
    import shutil
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "db").mkdir()
    source_script = Path(__file__).resolve().parents[1] / "scripts/cleanup_workspace.ps1"
    script = repo / "scripts/cleanup_workspace.ps1"
    shutil.copyfile(source_script, script)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    junk = repo / ".test-disposable"
    junk.mkdir()
    (junk / filename).write_text("test")
    cmd = ["powershell.exe", "-NoProfile", "-File", str(script)]
    preview = subprocess.run(cmd, capture_output=True, text=True)
    assert preview.returncode == 0, preview.stderr
    assert junk.exists()
    subprocess.run(["git", "-C", str(repo), "add", f".test-disposable/{filename}"], check=True)
    protected = subprocess.run(cmd + ["-Apply"], capture_output=True, text=True)
    assert protected.returncode != 0
    assert "tracked files" in protected.stderr
    assert (junk / filename).read_text() == "test"


@pytest.mark.skipif(__import__('sys').platform != 'win32', reason="Windows junction safety")
@pytest.mark.parametrize('case', ['cleanup_db', 'backup_root', 'backup_db'])
def test_maintenance_rejects_junction_destinations(tmp_path, case):
    import shutil
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    evidence = external / "argosy_before_example.db"
    evidence.write_bytes(b"must survive untouched")
    destination = tmp_path / "backup"
    if case == "cleanup_db":
        link = repo / "db"
        name = "cleanup_workspace.ps1"
        args = ["-Apply"]
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    else:
        (repo / "db").mkdir()
        name = "backup_to_sibling.ps1"
        args = ["-Destination", str(destination)]
        link = destination if case == "backup_root" else destination / "db"
        link.parent.mkdir(exist_ok=True)
    script = repo / "scripts" / name
    shutil.copyfile(Path(__file__).resolve().parents[1] / "scripts" / name, script)
    create = subprocess.run(["powershell.exe", "-NoProfile", "-Command",
        f"New-Item -ItemType Junction -Path '{link}' -Target '{external}'"], capture_output=True, text=True)
    assert create.returncode == 0, create.stderr
    result = subprocess.run(["powershell.exe", "-NoProfile", "-File", str(script), *args],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert "reparse point" in result.stderr.lower()
    assert evidence.read_bytes() == b"must survive untouched"
    assert list(external.iterdir()) == [evidence]
