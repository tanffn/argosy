"""Copy a stable transcript view under the same lock as writers and archivers."""
import argparse
import hashlib
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from argosy.services.backup_storage import plain_path
from argosy.services.transcript_archive import _plain, transcript_lock


def copy_transcripts(source: Path, destination: Path) -> int:
    source, destination = plain_path(source), plain_path(destination)
    if source == destination or source.is_relative_to(destination) or destination.is_relative_to(source):
        raise ValueError("Transcript backup must be a separate tree")
    if not source.exists():
        return 0
    count = 0
    with transcript_lock(source):
        pending = [source]
        while pending:
            for entry in pending.pop().iterdir():
                _plain(entry, source)
                if entry.is_dir():
                    pending.append(entry)
                    continue
                if entry == source / '_archive/.lock':
                    continue
                target = plain_path(destination / entry.relative_to(source))
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(entry, target)
                with entry.open('rb') as reader, target.open('rb') as copied:
                    if hashlib.file_digest(reader, 'sha256').digest() != hashlib.file_digest(copied, 'sha256').digest():
                        raise RuntimeError("Transcript copy verification failed")
                count += 1
    return count


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    print('Verified transcript files copied:', copy_transcripts(args.source, args.destination))
