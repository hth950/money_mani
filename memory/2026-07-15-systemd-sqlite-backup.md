# Systemd SQLite backup sandbox regression

- **Symptom:** The 03:30 KST backup timer was installed, but its first service run failed with `sqlite3.OperationalError: unable to open database file` during `source.backup(target)`.
- **Root cause:** `ProtectSystem=strict` exposed only the backup destination as writable. A controlled `systemd-run` reproduction showed that SQLite's online Backup API also requires the live database directory to be writable for lock/journal handling, despite opening the source with `mode=ro`.
- **Fix:** Keep the strict filesystem sandbox and add only `/srv/money-mani/shared/data` beside the backup directory to `ReadWritePaths`.
- **Regression test:** `tests/test_hermes_deployment.py::test_backup_unit_allows_only_required_sqlite_write_paths` protects the required paths and hardening directives.
- **Evidence:** The isolated reproduction failed without the data path and completed successfully with it; the Hermes deployment tests passed (16 tests).
- **Status:** DONE
