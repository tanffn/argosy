"""Windows subscription CLI launcher. Its private job closes when this process exits."""

import subprocess
import sys

from argosy.services.windows_job import join_worker_job

if __name__ == "__main__":
    join_worker_job()
    raise SystemExit(subprocess.call(sys.argv[1:]))
