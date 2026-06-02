"""
Atomic batch executor for fileops.

Execution model:
  1. PREPARE  — validate inputs, back up files that will change, write new
                content to temp files in the same directory (same filesystem
                guarantees os.replace() is atomic on POSIX).
  2. COMMIT   — os.replace() each temp file to its final path. Fast and atomic.
  3. ROLLBACK — if any step fails, reverse committed operations in reverse order
                and clean up all temp files.

os.replace() is used throughout instead of os.rename() because it is atomic
even when the destination already exists (POSIX rename(2) semantics).
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import warnings
from dataclasses import dataclass
from typing import Optional

from .differ import compute_diff, diff_strings
from .exceptions import ExecutorStateError, RollbackFailedWarning
from .models import BatchResult, BatchSpec, FileOperation, OperationResult, OperationType


@dataclass
class _Pending:
    """Internal state for one in-flight operation."""

    operation: FileOperation
    temp_path: Optional[str] = None    # staged content (CREATE, WRITE)
    backup_path: Optional[str] = None  # original content (WRITE, DELETE, MOVE dest)
    diff: Optional[str] = None         # computed before commit so dry-run works too
    mode: Optional[int] = None         # permission bits to stamp on the staged file
    committed: bool = False


def execute(spec: BatchSpec) -> BatchResult:
    """
    Execute a BatchSpec atomically.

    On dry_run=True: validates and computes diffs but writes nothing to disk.
    On failure: rolls back all committed operations and cleans up temp files.
    """
    executor = _Executor()
    return executor.run(spec)


class _Executor:
    def __init__(self) -> None:
        # In-batch working copy of file text, keyed by absolute path. Seeded by
        # every content-producing op (CREATE/WRITE/EDIT/INSERT) so that later
        # EDIT/INSERT ops on the same file compose against the batch's pending
        # state instead of re-reading the (stale) on-disk snapshot — otherwise a
        # WRITE followed by an EDIT to the same file would have the EDIT build
        # from old disk text and the last os.replace() would silently drop the
        # WRITE's content.
        self._working: dict[str, str] = {}
        # Permission bits to preserve per path. mkstemp() creates temp files as
        # 0600; without this the committed file would lose the original's mode
        # (e.g. an executable script's +x bit). Captured once per path so a
        # same-file chain stamps a consistent mode.
        self._modes: dict[str, int] = {}
        # Directories this batch created via makedirs, so rollback / dry-run can
        # remove the ones it made (only if still empty) — makedirs side effects
        # would otherwise survive a rollback and break the "all or nothing".
        self._created_dirs: list[str] = []
        # Snapshot the process umask so newly created files honor it (like a
        # normal open(path, "w")) rather than inheriting mkstemp's 0600.
        umask = os.umask(0)
        os.umask(umask)
        self._umask = umask

    def run(self, spec: BatchSpec) -> BatchResult:
        pending: list[_Pending] = []

        # ── Phase 1: Prepare ─────────────────────────────────────────────────
        try:
            for op in spec.operations:
                p = self._prepare(op)
                pending.append(p)
        except Exception as exc:
            self._cleanup(pending)
            self._remove_created_dirs()
            return BatchResult(
                success=False,
                results=[],
                rolled_back=False,
                error=f"Preparation failed: {exc}",
            )

        # ── Dry run: return diffs, touch nothing ──────────────────────────────
        if spec.dry_run:
            results = [
                OperationResult(operation=p.operation, success=True, diff=p.diff)
                for p in pending
            ]
            self._cleanup(pending)
            self._remove_created_dirs()
            return BatchResult(success=True, results=results)

        # ── Phase 2: Commit ───────────────────────────────────────────────────
        failed_index: Optional[int] = None
        commit_error: Optional[str] = None
        for i, p in enumerate(pending):
            try:
                self._commit(p)
                p.committed = True
            except Exception as exc:
                failed_index = i
                commit_error = str(exc)
                break

        if failed_index is not None:
            self._rollback(pending)
            # Report honestly: every op is now un-applied. The ops that committed
            # before the failure were reversed, the failing op carries its error,
            # and the rest were never attempted. None of them succeeded.
            results = []
            for i, p in enumerate(pending):
                if i < failed_index:
                    err = "rolled back after a later operation failed"
                elif i == failed_index:
                    err = commit_error
                else:
                    err = "not attempted (batch aborted)"
                results.append(
                    OperationResult(
                        operation=p.operation, success=False, diff=p.diff, error=err
                    )
                )
            return BatchResult(
                success=False,
                results=results,
                rolled_back=True,
                error=f"Commit failed: {commit_error}",
            )

        # ── Phase 3: Clean up backups ─────────────────────────────────────────
        self._cleanup_backups(pending)
        results = [
            OperationResult(operation=p.operation, success=True, diff=p.diff)
            for p in pending
        ]
        return BatchResult(success=True, results=results)

    # ── Preparation ───────────────────────────────────────────────────────────

    def _prepare(self, op: FileOperation) -> _Pending:
        p = _Pending(operation=op)
        path = op.path
        abspath = os.path.abspath(path)

        if op.type == OperationType.CREATE:
            if os.path.exists(path):
                raise FileExistsError(f"Cannot create: {path!r} already exists")
            dir_path = os.path.dirname(abspath)
            self._makedirs_tracked(dir_path)
            p.diff = compute_diff(op)
            p.mode = self._resolve_mode(abspath, path)
            self._working[abspath] = op.content or ""
            p.temp_path = self._stage(dir_path, op.content or "")

        elif op.type == OperationType.WRITE:
            dir_path = os.path.dirname(abspath)
            self._makedirs_tracked(dir_path)
            p.diff = compute_diff(op)
            p.mode = self._resolve_mode(abspath, path)
            if os.path.exists(path):
                p.backup_path = self._make_backup(path, dir_path)
            self._working[abspath] = op.content or ""
            p.temp_path = self._stage(dir_path, op.content or "")

        elif op.type in (OperationType.EDIT, OperationType.INSERT):
            # EDIT/INSERT derive new content from the existing file (or this
            # batch's pending state), then ride the same temp-file +
            # os.replace() staging path as WRITE.
            in_batch = abspath in self._working
            if not in_batch and not os.path.exists(path):
                raise FileNotFoundError(f"Cannot edit non-existent path: {path!r}")
            dir_path = os.path.dirname(abspath)
            before = self._working[abspath] if in_batch else self._read_text(path)
            after = self._apply_string_op(op, before)
            p.diff = diff_strings(path, before, after)
            p.mode = self._resolve_mode(abspath, path)
            self._working[abspath] = after
            # Back up the on-disk original only if there is one. If a prior op in
            # this batch created the file, it isn't on disk yet — rollback will
            # remove it rather than restore a backup.
            if os.path.exists(path):
                p.backup_path = self._make_backup(path, dir_path)
            p.temp_path = self._stage(dir_path, after)

        elif op.type == OperationType.DELETE:
            if not os.path.exists(path):
                raise FileNotFoundError(f"Cannot delete non-existent path: {path!r}")
            dir_path = os.path.dirname(abspath)
            p.diff = compute_diff(op)
            p.backup_path = self._make_backup(path, dir_path)

        elif op.type == OperationType.MOVE:
            if not os.path.exists(path):
                raise FileNotFoundError(f"Cannot move non-existent path: {path!r}")
            dest = op.destination
            if dest is None:
                raise ExecutorStateError("destination missing")
            dest_dir = os.path.dirname(os.path.abspath(dest))
            self._makedirs_tracked(dest_dir)
            if os.path.exists(dest):
                p.backup_path = self._make_backup(dest, dest_dir)

        return p

    def _stage(self, dir_path: str, content: str) -> str:
        """Write ``content`` to a temp file in ``dir_path`` and return its path."""
        fd, temp = tempfile.mkstemp(dir=dir_path, prefix=".fileops_tmp_")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        return temp

    def _make_backup(self, path: str, dir_path: str) -> str:
        fd, backup = tempfile.mkstemp(dir=dir_path, prefix=".fileops_bak_")
        os.close(fd)
        shutil.copy2(path, backup)  # copy2 preserves mode for accurate rollback
        return backup

    def _resolve_mode(self, abspath: str, path: str) -> int:
        """
        Permission bits the committed file should carry. Captured once per path
        (and reused across a same-file chain): the on-disk mode for an existing
        file, or the umask-respecting default for a newly created one.
        """
        if abspath in self._modes:
            return self._modes[abspath]
        if os.path.exists(path):
            mode = stat.S_IMODE(os.stat(path).st_mode)
        else:
            mode = 0o666 & ~self._umask
        self._modes[abspath] = mode
        return mode

    def _makedirs_tracked(self, dir_path: str) -> None:
        """makedirs(dir_path), recording any directories we actually create."""
        target = os.path.abspath(dir_path)
        missing: list[str] = []
        cur = target
        while cur and not os.path.exists(cur):
            missing.append(cur)
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
        os.makedirs(target, exist_ok=True)
        for d in missing:
            if d not in self._created_dirs:
                self._created_dirs.append(d)

    def _read_text(self, path: str) -> str:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except UnicodeDecodeError:
            raise ValueError(f"Cannot edit {path!r}: file contains non-UTF-8 content")

    def _apply_string_op(self, op: FileOperation, text: str) -> str:
        """Apply an EDIT or INSERT to ``text``, enforcing exact-match rules."""
        if op.type == OperationType.EDIT:
            old = op.old_string
            new = op.new_string
            if old is None or new is None:
                raise ExecutorStateError("edit requires old_string and new_string")
            count = text.count(old)
            if count == 0:
                raise ValueError(f"old_string not found in {op.path!r}")
            if count > 1 and not op.replace_all:
                raise ValueError(
                    f"old_string found {count} times in {op.path!r}; "
                    f"set replace_all=True or make the match unique"
                )
            return text.replace(old, new) if op.replace_all else text.replace(old, new, 1)

        # INSERT
        anchor = op.anchor
        if anchor is None or op.content is None:
            raise ExecutorStateError("insert requires anchor and content")
        count = text.count(anchor)
        if count == 0:
            raise ValueError(f"anchor not found in {op.path!r}")
        if count > 1:
            raise ValueError(
                f"anchor found {count} times in {op.path!r}; make the anchor unique"
            )
        if op.position == "before":
            return text.replace(anchor, op.content + anchor, 1)
        return text.replace(anchor, anchor + op.content, 1)

    # ── Commit ────────────────────────────────────────────────────────────────

    def _commit(self, p: _Pending) -> None:
        op = p.operation
        if op.type in (
            OperationType.CREATE,
            OperationType.WRITE,
            OperationType.EDIT,
            OperationType.INSERT,
        ):
            if p.temp_path is None:
                raise ExecutorStateError("temp_path missing")
            if p.mode is not None:
                # Best-effort: preserve the original/umask mode. Never let a mode
                # failure abort an otherwise-valid commit.
                try:
                    os.chmod(p.temp_path, p.mode)
                except OSError:
                    pass
            os.replace(p.temp_path, op.path)  # atomic on POSIX
            p.temp_path = None
        elif op.type == OperationType.DELETE:
            os.unlink(op.path)
        elif op.type == OperationType.MOVE:
            if op.destination is None:
                raise ExecutorStateError("destination missing")
            os.replace(op.path, op.destination)  # atomic on POSIX, same fs

    # ── Rollback ──────────────────────────────────────────────────────────────

    def _rollback(self, pending: list[_Pending]) -> None:
        """Reverse committed operations in reverse order, then clean up."""
        for p in reversed(pending):
            if not p.committed:
                continue
            op = p.operation
            try:
                if op.type in (
                    OperationType.CREATE,
                    OperationType.WRITE,
                    OperationType.EDIT,
                    OperationType.INSERT,
                ):
                    if p.backup_path and os.path.exists(p.backup_path):
                        os.replace(p.backup_path, op.path)
                        p.backup_path = None
                    elif os.path.exists(op.path):
                        os.unlink(op.path)  # was newly created, remove it

                elif op.type == OperationType.DELETE:
                    if p.backup_path and os.path.exists(p.backup_path):
                        os.replace(p.backup_path, op.path)
                        p.backup_path = None

                elif op.type == OperationType.MOVE:
                    if op.destination is None:
                        raise ExecutorStateError("destination missing")
                    if os.path.exists(op.destination):
                        os.replace(op.destination, op.path)
                    if p.backup_path and os.path.exists(p.backup_path):
                        os.replace(p.backup_path, op.destination)
                        p.backup_path = None

            except OSError as exc:
                warnings.warn(
                    f"Rollback step failed for {op.path!r}: {exc}",
                    RollbackFailedWarning,
                    stacklevel=2,
                )

        self._cleanup(pending)
        self._remove_created_dirs()

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def _cleanup(self, pending: list[_Pending]) -> None:
        """Remove any remaining temp and backup files."""
        for p in pending:
            for attr in ("temp_path", "backup_path"):
                path = getattr(p, attr)
                if path and os.path.exists(path):
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
                    setattr(p, attr, None)

    def _cleanup_backups(self, pending: list[_Pending]) -> None:
        for p in pending:
            if p.backup_path and os.path.exists(p.backup_path):
                try:
                    os.unlink(p.backup_path)
                except OSError:
                    pass

    def _remove_created_dirs(self) -> None:
        """Remove directories this batch created, deepest first, only if empty."""
        for d in sorted(set(self._created_dirs), key=len, reverse=True):
            try:
                os.rmdir(d)
            except OSError:
                pass  # not empty (holds a committed file) or already gone
        self._created_dirs.clear()
