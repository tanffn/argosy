# Workspace cleanup and copy backup

```powershell
# Preview; nothing is removed:
.\scripts\cleanup_workspace.ps1
# Move verified untracked test roots / old loose DB snapshots to a sibling quarantine:
.\scripts\cleanup_workspace.ps1 -Apply
# Copy source and runtime evidence; never purge destination-only files:
.\scripts\backup_to_sibling.ps1 -Quiet
```

Cleanup leaves managed `backups/`, live SQLite files, runtime evidence, and tracked
files intact. It rejects root reparse points and tracked files (including Unicode
paths); same-volume directory renames do not traverse test-created links. It
refuses while tests are running. A quarantine `manifest.json` records source and
destination paths; restore selected items to their original paths only if absent.
Access-denied items remain in place and the command reports failure, not success.
Run future tests under `--basetemp=tmp/pytest-<unique-run-name>`, not root `.test-*`.

The backup is a **copy**, not a mirror. Default destination is the sibling
`financial-advisor-backup`. Source-only changes are copied; destination-only files
survive. Dependencies, git metadata, test scratch and redundant managed backups
are excluded. Local secrets/runtime data stay in this local backup, never in git.

**Database restore source: `db/argosy-consistent.db.gz` in the backup.** This is
created using SQLite's online backup API, includes committed WAL transactions,
passes `PRAGMA quick_check`, and atomically replaces the previous verified snapshot.
Compression is SHA256 round-trip checked. Errors fail the backup; there is no
raw-copy fallback. Daily managed snapshots also use `.db.gz`; retention counts
distinct dates, accepting legacy `.db` files during transition.
Do not restore the legacy `db/argosy.db` or its sidecars that older backup versions
may have left in the destination. Decompress to a NEW scratch path first:

```powershell
.\.venv\Scripts\python.exe scripts\backup_sqlite.py ..\financial-advisor-backup\db\argosy-consistent.db.gz tmp\restore-drill.db --restore
```

This verifies SQLite integrity and refuses to overwrite an existing destination.
Stop all Argosy writers before any production restoration,
preserve the current live DB plus sidecars together outside `db/`, then copy the
verified snapshot as `db/argosy.db` without old WAL/SHM files.

### Transcript history

The daily backup loop checks whether weekly archival is due. Files whose bundle
date AND modification time are older than 30 days move into verified weekly ZIPs
under `transcripts/_archive/<user>/<ISO-year>-W<week>.zip`. Recent/modified files
stay loose. A cross-process lock coordinates writers, archival and backup copying.
ZIPs are verified and durably published before matching originals are removed;
history is retained, not expired. Replay and transcript downloads read ZIPs without
extracting them. The next scheduled backup catches up after downtime.

An optional first pass (otherwise wait for the daily backup):

```powershell
.\.venv\Scripts\python.exe scripts\archive_transcripts.py
```

### Audited one-time disk reclaim

```powershell
.\scripts\reclaim_storage.ps1          # verify + preview only
.\scripts\reclaim_storage.ps1 -Apply   # refresh recovery copy, recheck, remove
```

The script targets the specifically audited obsolete probe/migration/restore-test
DBs and raw dated snapshots that have byte-identical `.db.gz` copies. It refuses
changed-size obsolete files, tracked paths, links, open files and nonempty WALs.
It never recursively deletes a directory. Obsolete probe/migration DBs are deleted
permanently; this is not a quarantine. Retained dated snapshots can be restored
from their compressed copies, and a fresh current-DB backup precedes deletion.
The fresh copy destination is `../financial-advisor-backup-fresh-20260923`.
The UI cache, older sibling backup and Drive upload cache are deliberately outside
this script's scope. Copy-only backup never purges old destination-only clutter.

Existing `.test-*` folders owned by older sandbox identities can deny the current
Windows user access. The cleanup does not broaden ACLs or silently discard them;
an administrator must grant access on those exact remaining test roots before
rerunning cleanup. Do not reset permissions on the whole repository.
