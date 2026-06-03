# Changelog

All notable changes to this project are documented here. This project adheres to
[Semantic Versioning](https://semver.org/).

## 0.4.0

Durability, safety, and capability pass, plus project-health tooling.

### Added

- Binary content: `CREATE`/`WRITE` accept `content_encoding: "base64"` to write
  raw bytes (images, archives), not just UTF-8 text.
- API path confinement: set `FILEOPS_ROOT` to require every operation path (and
  MOVE destination) to resolve inside that directory; escaping requests get 403.
  Allowed CORS origins are configurable via `FILEOPS_CORS_ORIGINS`.
- Continuous integration: GitHub Actions runs ruff, `mypy --strict`, and pytest
  with coverage across Python 3.11-3.14.

### Changed

- **Crash durability.** Staged file data is `fsync`'d before the atomic rename
  and the parent directory is `fsync`'d after, so a crash can't leave a
  renamed-but-empty file or a lost rename.
- **Symlinks are refused.** Operating on a symlinked target (any operation,
  including a MOVE source or destination) now fails instead of silently
  replacing the link with a regular file. Target the real path instead.

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
