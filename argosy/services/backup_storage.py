"""Atomic SQLite recovery artifacts, optionally gzip-compressed and round-trip checked."""
import gzip
import hashlib
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path


def plain_path(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    for entry in [path, *path.parents]:
        if entry.is_symlink() or entry.is_junction():
            raise ValueError("Backup path contains a link/reparse point")
    return path


def _temporary(parent: Path, suffix: str) -> Path:
    handle, name = tempfile.mkstemp(prefix="argosy-snapshot-", suffix=suffix, dir=parent)
    os.close(handle)
    return Path(name)


def _digest(stream) -> str:
    digest = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(block)
    return digest.hexdigest()


def durable_replace(source: Path, destination: Path) -> None:
    """Flush completed bytes, then publish with write-through metadata semantics."""
    with source.open("r+b") as handle:
        os.fsync(handle.fileno())
    if os.name == "nt":
        import ctypes
        move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong]
        move.restype = ctypes.c_int
        if not move(str(source), str(destination), 0x1 | 0x8):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.replace(source, destination)
        fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _check(database: Path) -> None:
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)) as conn:
        if conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise RuntimeError("SQLite snapshot failed quick_check")


def _publish_gzip(source: Path, destination: Path) -> dict:
    temporary = _temporary(destination.parent, ".gz")
    try:
        digest = hashlib.sha256()
        with source.open("rb") as reader, gzip.open(temporary, "wb", compresslevel=6) as writer:
            for block in iter(lambda: reader.read(1024 * 1024), b""):
                digest.update(block)
                writer.write(block)
        with gzip.open(temporary, "rb") as reader:
            if _digest(reader) != digest.hexdigest():
                raise RuntimeError("Compressed backup failed round-trip SHA256 verification")
        durable_replace(temporary, destination)
        return {"sha256_uncompressed": digest.hexdigest(), "original_bytes": source.stat().st_size,
                "stored_bytes": destination.stat().st_size, "path": str(destination)}
    finally:
        temporary.unlink(missing_ok=True)


def snapshot(source: Path, destination: Path) -> dict:
    """Online SQLite backup, never a raw-copy fallback. Failure preserves prior artifact."""
    source = plain_path(source).resolve(strict=True)
    destination = plain_path(destination)
    if source == destination:
        raise ValueError("Backup destination must differ from the live database")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if any(Path(str(destination) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise ValueError("Destination has SQLite sidecars; close its readers before replacing it")
    temporary = _temporary(destination.parent, ".db")
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as reader:
            with closing(sqlite3.connect(temporary)) as writer:
                reader.backup(writer)
                writer.execute("PRAGMA journal_mode=DELETE")
        _check(temporary)
        if destination.suffix == ".gz":
            result = _publish_gzip(temporary, destination)
        else:
            durable_replace(temporary, destination)
            result = {"path": str(destination), "stored_bytes": destination.stat().st_size}
        return {**result, "quick_check": "ok"}
    finally:
        temporary.unlink(missing_ok=True)


def copy_artifact(source: Path, destination: Path) -> None:
    """Publish a byte-verified offsite copy without overwriting through links."""
    source, destination = plain_path(source), plain_path(destination)
    if source == destination:
        raise ValueError("Offsite copy must be separate from source")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary(destination.parent, '.copy')
    try:
        with source.open('rb') as reader, temporary.open('wb') as writer:
            shutil.copyfileobj(reader, writer)
        with source.open('rb') as reader, temporary.open('rb') as copied:
            if _digest(reader) != _digest(copied):
                raise RuntimeError("Offsite copy failed byte verification")
        plain_path(destination)
        durable_replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def compress_snapshot(source: Path) -> dict:
    """Verify and compress an inactive snapshot. Leave original deletion to the caller."""
    source = plain_path(source).resolve(strict=True)
    if source.suffix != ".db":
        raise ValueError("Expected a .db snapshot")
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(source) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ValueError("Snapshot has active SQLite sidecars; use online backup instead")
    before = source.stat()
    _check(source)
    destination = source.with_suffix(".db.gz")
    if destination.exists():
        raise FileExistsError("Compressed snapshot already exists; verify it before replacing anything")
    result = _publish_gzip(source, destination)
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError("Source changed during compression; retain original and inspect compressed copy")
    return {**result, "quick_check": "ok"}


def restore_snapshot(source: Path, destination: Path) -> None:
    """Restore a compressed artifact to a NEW file, validate before publishing it."""
    source = plain_path(source).resolve(strict=True)
    destination = plain_path(destination)
    if destination.exists() or any(Path(str(destination) + s).exists() for s in ("-wal", "-shm", "-journal")):
        raise FileExistsError("Restore only to a new path without SQLite sidecars")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary(destination.parent, ".db")
    try:
        with gzip.open(source, "rb") as reader, temporary.open("wb") as writer:
            shutil.copyfileobj(reader, writer)
        _check(temporary)
        # Hard-link creation is atomic and fails if another process created dest.
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
