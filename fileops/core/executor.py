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
        # In-batch working copy of file text, keyed by absolute path. Lets
        # successive EDIT/INSERT ops on the same file compose, instead of each
        # rebuilding from the (unchanging) on-disk snapshot and clobbering the
        # prior op at commit time (last os.replace() would otherwise win).
        self._working: dict[str, str] = {}

    def run(self, spec: BatchSpec) -> BatchResult:
        pending: list[_Pending] = []

        # ── Phase 1: Prepare ─────────────────────────────────────────────────
        try:
            for op in spec.operations:
                p = self._prepare(op)
                pending.append(p)
        except Exception as exc:
            self._cleanup(pending)
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
            return BatchResult(success=True, results=results)

        # ── Phase 2: Commit ───────────────────────────────────────────────────
        results: list[OperationResult] = []
        try:
            for p in pending:
                self._commit(p)
                p.committed = True
                results.append(
                    OperationResult(operation=p.operation, success=True, diff=p.diff)
                )
        except Exception as exc:
            self._rollback(pending)
            return BatchResult(
                success=False,
                results=results,
                rolled_back=True,
                error=f"Commit failed: {exc}",
            )

        # ── Phase 3: Clean up backups ─────────────────────────────────────────
        self._cleanup_backups(pending)
        return BatchResult(success=True, results=results)

    # ── Preparation ───────────────────────────────────────────────────────────

    def _prepare(self, op: FileOperation) -> _Pending:
        p = _Pending(operation=op)
        path = op.path

        if op.type == OperationType.CREATE:
            if os.path.exists(path):
                raise FileExistsError(f"Cannot create: {path!r} already exists")
            dir_path = os.path.dirname(os.path.abspath(path))
            os.makedirs(dir_path, exist_ok=True)
            p.diff = compute_diff(op)
            fd, temp = tempfile.mkstemp(dir=dir_path, prefix=".fileops_tmp_")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(op.content or "")
            p.temp_path = temp

        elif op.type == OperationType.WRITE:
            dir_path = os.path.dirname(os.path.abspath(path))
            os.makedirs(dir_path, exist_ok=True)
            p.diff = compute_diff(op)
            if os.path.exists(path):
                p.backup_path = self._make_backup(path, dir_path)
            fd, temp = tempfile.mkstemp(dir=dir_path, prefix=".fileops_tmp_")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(op.content or "")
            p.temp_path = temp

        elif op.type in (OperationType.EDIT, OperationType.INSERT):
            # EDIT/INSERT derive new content from the existing file, then ride
            # the same temp-file + os.replace() staging path as WRITE.
            abspath = os.path.abspath(path)
            if not os.path.exists(path):
                raise FileNotFoundError(f"Cannot edit non-existent path: {path!r}")
            dir_path = os.path.dirname(abspath)
            # Build on this batch's prior edits to the same file, if any.
            before = self._working[abspath] if abspath in self._working else self._read_text(path)
            after = self._apply_string_op(op, before)
            p.diff = diff_strings(path, before, after)
            self._working[abspath] = after
            # Each op backs up the on-disk original (unchanged during prepare),
            # so rollback restores the original regardless of commit order.
            p.backup_path = self._make_backup(path, dir_path)
            fd, temp = tempfile.mkstemp(dir=dir_path, prefix=".fileops_tmp_")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(after)
            p.temp_path = temp

        elif op.type == OperationType.DELETE:
            if not os.path.exists(path):
                raise FileNotFoundError(f"Cannot delete non-existent path: {path!r}")
            dir_path = os.path.dirname(os.path.abspath(path))
            p.diff = compute_diff(op)
            p.backup_path = self._make_backup(path, dir_path)

        elif op.type == OperationType.MOVE:
            if not os.path.exists(path):
                raise FileNotFoundError(f"Cannot move non-existent path: {path!r}")
            dest = op.destination
            if dest is None:
                raise ExecutorStateError("destination missing")
            dest_dir = os.path.dirname(os.path.abspath(dest))
            os.makedirs(dest_dir, exist_ok=True)
            if os.path.exists(dest):
                p.backup_path = self._make_backup(dest, dest_dir)

        return p

    def _make_backup(self, path: str, dir_path: str) -> str:
        fd, backup = tempfile.mkstemp(dir=dir_path, prefix=".fileops_bak_")
        os.close(fd)
        shutil.copy2(path, backup)
        return backup

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
