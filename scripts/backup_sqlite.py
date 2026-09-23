"""Create an atomic, checked online SQLite snapshot (including committed WAL data)."""
import argparse
import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path


def snapshot(source: Path, destination: Path) -> None:
    source = source.resolve(strict=True)
    destination = destination.resolve()
    if source == destination:
        raise ValueError("Backup destination must differ from the live database")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if any(Path(str(destination) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise ValueError("Destination has SQLite sidecars; close its readers before replacing it")
    handle, name = tempfile.mkstemp(prefix="argosy-snapshot-", suffix=".db", dir=destination.parent)
    os.close(handle)
    temporary = Path(name)
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as reader:
            with closing(sqlite3.connect(temporary)) as writer:
                reader.backup(writer)
                result = writer.execute("PRAGMA quick_check").fetchall()
                if result != [("ok",)]:
                    raise RuntimeError("SQLite snapshot failed quick_check")
                writer.execute("PRAGMA journal_mode=DELETE")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Verified SQLite snapshot: {destination} ({destination.stat().st_size:,} bytes; quick_check=ok)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    snapshot(args.source, args.destination)
