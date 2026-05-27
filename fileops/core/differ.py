"""
Unified diff generation for fileops operations.

Uses stdlib difflib only — no external dependencies.
"""

import difflib
import os
from typing import Optional

from .models import FileOperation, OperationType


def compute_diff(op: FileOperation) -> Optional[str]:
    """
    Compute a unified diff for an operation, reading the current file state
    from disk. Returns None for MOVE only — DELETE produces a deletion diff
    showing all removed lines.
    """
    if op.type == OperationType.DELETE:
        return _deletion_diff(op.path)

    if op.type == OperationType.MOVE:
        return None  # rename only, no content change

    # CREATE or WRITE — fail fast if the existing file is not UTF-8 text
    try:
        before_lines = _read_lines(op.path)
    except UnicodeDecodeError:
        raise ValueError(
            f"Cannot overwrite {op.path!r}: file contains non-UTF-8 content"
        )

    after_lines = (op.content or "").splitlines(keepends=True)

    label_before = f"a/{op.path}" if os.path.exists(op.path) else "/dev/null"
    label_after = f"b/{op.path}"

    diff = list(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=label_before,
            tofile=label_after,
        )
    )

    return "".join(diff) if diff else None


def _deletion_diff(path: str) -> Optional[str]:
    if not os.path.exists(path):
        return None
    try:
        before_lines = _read_lines(path)
    except UnicodeDecodeError:
        return None  # binary file — no text diff to show
    diff = list(
        difflib.unified_diff(
            before_lines,
            [],
            fromfile=f"a/{path}",
            tofile="/dev/null",
        )
    )
    return "".join(diff) if diff else None


def _read_lines(path: str) -> list[str]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.readlines()
    except OSError:
        return []
    # UnicodeDecodeError propagates to the caller
