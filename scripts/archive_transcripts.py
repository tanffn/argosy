"""Archive transcripts older than 30 days; normally invoked by the daily backup job."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from argosy.config import get_settings
from argosy.services.transcript_archive import archive_transcripts

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="Run before the weekly interval is due")
    args = parser.parse_args()
    print(json.dumps(archive_transcripts(Path(get_settings().home) / "transcripts", force=args.force)))
