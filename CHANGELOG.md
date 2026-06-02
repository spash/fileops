# Changelog

All notable changes to this project are documented here. This project adheres to
[Semantic Versioning](https://semver.org/).

## 0.3.0

Hardening pass: five correctness and safety fixes surfaced by adversarial
testing. Each is covered by a regression test in `tests/test_hardening.py`.

### Breaking

- `BatchSpec` now rejects unknown top-level keys (`extra="forbid"`). A spec with
  a typo'd flag such as `drz_run` is now an error instead of being silently
  ignored — previously this could execute a batch the caller meant to preview.

### Fixed

- **Same-file composition.** A `WRITE`/`CREATE` followed by an `EDIT`/`INSERT` on
  the same file within one batch now composes against the batch's pending
  content instead of stale on-disk text. Previously the first operation was
  silently dropped at commit while the batch reported success.
- **Permission bits preserved.** `WRITE`/`EDIT`/`INSERT` keep the file's original
  mode (e.g. a script's executable bit). Newly created files honor the process
  umask rather than the restrictive `0600` of the underlying temp file.
- **Atomic directory rollback.** Directories created via `makedirs` are removed
  (when left empty) if the batch rolls back, so a failed batch leaves no
  orphaned directories; dry-run no longer creates them at all.
- **Honest result reporting.** After a rollback, reverted operations report
  `success=False`, the failing operation carries its error message, and the
  remaining operations are labeled as not attempted.

## 0.2.0

- Add `EDIT` and `INSERT` operations for surgical file changes.
- Add tests for commit-phase rollback, binary edits, CLI, API, and edge cases.
