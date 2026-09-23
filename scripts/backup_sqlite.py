"""Create an atomic, checked online SQLite snapshot (including committed WAL data)."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from argosy.services.backup_storage import restore_snapshot, snapshot

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--restore", action="store_true", help="Verify/decompress to a NEW database path")
    args = parser.parse_args()
    if args.restore:
        restore_snapshot(args.source, args.destination)
        print("Restored and verified (quick_check=ok):", args.destination)
    else:
        print(json.dumps(snapshot(args.source, args.destination), indent=2))
