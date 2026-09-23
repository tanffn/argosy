"""Weekly ZIP storage for old phase bundles, with transparent read-through."""
import hashlib
import json
import os
import tempfile
import time
import zipfile
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from argosy.services.backup_storage import durable_replace

MAX_FILE_BYTES = 64 * 1024 * 1024


def _plain(path: Path, root: Path) -> Path:
    path = Path(os.path.abspath(path))
    root = Path(os.path.abspath(root))
    if not path.is_relative_to(root):
        raise ValueError("Transcript path escapes root")
    for entry in [path, *path.parents]:
        if entry.is_symlink() or entry.is_junction():
            raise ValueError("Transcript path contains a link")
    return path


def _archive_path(root: Path, user: str, day: date) -> Path:
    year, week, _ = day.isocalendar()
    return _plain(root / "_archive" / user / f"{year}-W{week:02d}.zip", root)


def read_bundle_file(root: Path, path: Path) -> bytes:
    root = root.absolute()
    path = _plain(path.absolute(), root)
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("Transcript exceeds size limit")
        return path.read_bytes()
    except FileNotFoundError:
        pass
    relative = path.relative_to(root)
    if len(relative.parts) != 4:
        raise FileNotFoundError(path)
    user, day, _, _ = relative.parts
    archive = _archive_path(root, user, date.fromisoformat(day))
    try:
        with zipfile.ZipFile(archive) as bundle:
            info = bundle.getinfo(relative.as_posix())
            if info.file_size > MAX_FILE_BYTES:
                raise ValueError("Archived transcript exceeds size limit")
            return bundle.read(info)
    except KeyError as exc:
        raise FileNotFoundError(path) from exc


@contextmanager
def transcript_lock(root: Path, *, timeout_s=120):
    root = _plain(root.absolute(), root.absolute())
    root.mkdir(parents=True, exist_ok=True)
    archive_root = _plain(root / "_archive", root)
    archive_root.mkdir(exist_ok=True)
    lock = _plain(archive_root / ".lock", root)
    with lock.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Transcript store is busy; nothing removed") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def archive_transcripts(root: Path, *, now: datetime | None = None, force=False) -> dict:
    root = _plain(root.absolute(), root.absolute())
    if not root.exists():
        return {"status": "no_transcripts", "archived_files": 0}
    with transcript_lock(root):
        return _archive_transcripts_locked(root, now=now, force=force)


def _archive_transcripts_locked(root: Path, *, now: datetime | None = None, force=False) -> dict:
    """Keep 30 days loose; run at most weekly, catching up after downtime.

    Publish verified ZIPs before unlinking byte-matched originals. No extraction,
    arbitrary tree deletion, or deletion of modified/new files.
    """
    now = now or datetime.now(UTC)
    root = root.absolute()
    _plain(root, root)
    if not root.exists():
        return {"status": "no_transcripts", "archived_files": 0}
    archive_root = _plain(root / "_archive", root)
    archive_root.mkdir(exist_ok=True)
    stamp = _plain(archive_root / "last_success.json", root)
    if stamp.exists() and not force:
        previous = datetime.fromisoformat(json.loads(stamp.read_text())['completed_at'])
        if now - previous < timedelta(days=7):
            return {"status": "not_due", "archived_files": 0}
    cutoff = now - timedelta(days=30)
    groups: dict[Path, list[Path]] = {}
    for user in root.iterdir():
        if user.name.startswith("_") or not user.is_dir():
            continue
        _plain(user, root)
        for folder in user.iterdir():
            try:
                day = date.fromisoformat(folder.name)
            except ValueError:
                continue
            if day >= cutoff.date() or not folder.is_dir():
                continue
            _plain(folder, root)
            for bundle in folder.iterdir():
                _plain(bundle, root)
                if not bundle.is_dir():
                    continue
                for file in bundle.iterdir():
                    _plain(file, root)
                    if not file.is_file() or file.stat().st_mtime >= cutoff.timestamp():
                        continue
                    if file.stat().st_size > MAX_FILE_BYTES:
                        raise ValueError("Transcript exceeds archival size limit")
                    groups.setdefault(_archive_path(root, user.name, day), []).append(file)
    removed = 0
    for archive, files in groups.items():
        archive.parent.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(prefix=".transcripts-", suffix=".zip", dir=archive.parent)
        os.close(handle)
        temporary = Path(name)
        hashes = {}
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as writer:
                existing = set()
                if archive.exists():
                    with zipfile.ZipFile(archive) as old:
                        for info in old.infolist():
                            if info.file_size > MAX_FILE_BYTES or info.filename in existing:
                                raise ValueError("Invalid existing transcript archive")
                            writer.writestr(info.filename, old.read(info))
                            existing.add(info.filename)
                for file in files:
                    _plain(file, root)
                    data = file.read_bytes()
                    if len(data) > MAX_FILE_BYTES:
                        raise ValueError("Transcript grew beyond size limit")
                    member = file.relative_to(root).as_posix()
                    hashes[member] = hashlib.sha256(data).digest()
                    if member not in existing:
                        writer.writestr(member, data)
            with zipfile.ZipFile(temporary) as check:
                if check.testzip() is not None:
                    raise RuntimeError("Transcript ZIP failed CRC verification")
                for member, digest in hashes.items():
                    if hashlib.sha256(check.read(member)).digest() != digest:
                        raise RuntimeError("Archive conflict; originals preserved")
            durable_replace(temporary, archive)
            for file in files:
                _plain(file, root)
                member = file.relative_to(root).as_posix()
                if file.stat().st_mtime < cutoff.timestamp() and hashlib.sha256(file.read_bytes()).digest() == hashes[member]:
                    file.unlink()
                    removed += 1
            for folder in sorted({p.parent for p in files} | {p.parent.parent for p in files}, key=lambda p: len(p.parts), reverse=True):
                _plain(folder, root)
                if folder.is_dir() and not any(folder.iterdir()):
                    folder.rmdir()
        finally:
            temporary.unlink(missing_ok=True)
    handle, name = tempfile.mkstemp(prefix=".last-success-", dir=archive_root)
    os.close(handle)
    temporary = Path(name)
    try:
        temporary.write_text(json.dumps({"completed_at": now.isoformat()}), encoding="utf-8")
        durable_replace(temporary, stamp)
    finally:
        temporary.unlink(missing_ok=True)
    return {"status": "archived", "archives": len(groups), "archived_files": removed}
