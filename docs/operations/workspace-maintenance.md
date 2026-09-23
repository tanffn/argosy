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

**Database restore source: `db/argosy-consistent.db` in the backup.** This is
created using SQLite's online backup API, includes committed WAL transactions,
passes `PRAGMA quick_check`, and atomically replaces the previous verified snapshot.
Do not restore the legacy `db/argosy.db` or its sidecars that older backup versions
may have left in the destination. Stop all Argosy writers before any restoration,
preserve the current live DB plus sidecars together outside `db/`, then copy the
verified snapshot as `db/argosy.db` without old WAL/SHM files.

Existing `.test-*` folders owned by older sandbox identities can deny the current
Windows user access. The cleanup does not broaden ACLs or silently discard them;
an administrator must grant access on those exact remaining test roots before
rerunning cleanup. Do not reset permissions on the whole repository.
