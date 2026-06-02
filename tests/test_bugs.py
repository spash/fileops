"""
Regression tests for each identified bug.
Each class names the bug it covers and should FAIL before the fix is applied.
"""

import os
import tempfile
import unittest
from pathlib import Path

from fileops.core import execute
from fileops.core.models import BatchSpec, FileOperation, OperationType

# ── Helpers (duplicated from test_executor to keep this file self-contained) ──

def spec(*ops, dry_run=False):
    return BatchSpec(operations=list(ops), dry_run=dry_run)

def op_write(path, content):
    return FileOperation(type=OperationType.WRITE, path=path, content=content)

def op_create(path, content):
    return FileOperation(type=OperationType.CREATE, path=path, content=content)

def op_delete(path):
    return FileOperation(type=OperationType.DELETE, path=path)

def op_move(path, dest):
    return FileOperation(type=OperationType.MOVE, path=path, destination=dest)


# ── Bug #1: CREATE silently overwrites ────────────────────────────────────────

class TestCreateNoOverwrite(unittest.TestCase):
    """CREATE on an existing file should fail, not silently clobber it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def path(self, name):
        return os.path.join(self.tmp, name)

    def test_create_existing_file_fails(self):
        target = self.path("existing.txt")
        Path(target).write_text("original")
        result = execute(spec(op_create(target, "new content")))
        self.assertFalse(result.success)

    def test_create_existing_file_does_not_overwrite(self):
        target = self.path("existing.txt")
        Path(target).write_text("original")
        execute(spec(op_create(target, "new content")))
        self.assertEqual(Path(target).read_text(), "original")

    def test_create_nonexistent_still_works(self):
        """Ensure the fix doesn't break the normal CREATE case."""
        target = self.path("new.txt")
        result = execute(spec(op_create(target, "hello")))
        self.assertTrue(result.success)
        self.assertEqual(Path(target).read_text(), "hello")

    def test_create_existing_leaves_no_temp_files(self):
        target = self.path("existing.txt")
        Path(target).write_text("original")
        execute(spec(op_create(target, "new content")))
        leftovers = [f for f in os.listdir(self.tmp) if f.startswith(".fileops_")]
        self.assertEqual(leftovers, [])


# ── Bug #4: rolled_back misreported for prepare-phase failures ────────────────

class TestRolledBackSemantics(unittest.TestCase):
    """rolled_back=True must only be set when something was actually committed."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def path(self, name):
        return os.path.join(self.tmp, name)

    def test_single_prepare_failure_not_rolled_back(self):
        """DELETE nonexistent — prepare fails, nothing committed."""
        result = execute(spec(op_delete(self.path("nope.txt"))))
        self.assertFalse(result.success)
        self.assertFalse(result.rolled_back)

    def test_multi_op_prepare_failure_not_rolled_back(self):
        """First op prepares OK, second fails — still nothing committed."""
        a = self.path("a.txt")
        Path(a).write_text("original")
        result = execute(spec(
            op_write(a, "modified"),
            op_delete(self.path("nope.txt")),
        ))
        self.assertFalse(result.success)
        self.assertFalse(result.rolled_back)
        # Original file must be untouched (commit never happened)
        self.assertEqual(Path(a).read_text(), "original")


# ── Bug #6: makedirs called unconditionally ───────────────────────────────────

class TestMakedirsScope(unittest.TestCase):
    """makedirs must not create directories for DELETE or MOVE-source operations."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def test_delete_nonexistent_does_not_create_parent_dir(self):
        ghost_dir = self.path("ghost_dir")
        target = os.path.join(ghost_dir, "file.txt")
        execute(spec(op_delete(target)))
        self.assertFalse(os.path.exists(ghost_dir))

    def test_move_nonexistent_source_does_not_create_source_dir(self):
        ghost_dir = self.path("ghost_src_dir")
        src = os.path.join(ghost_dir, "src.txt")
        dst = self.path("dst.txt")
        execute(spec(op_move(src, dst)))
        self.assertFalse(os.path.exists(ghost_dir))


# ── Bug #5: UnicodeDecodeError swallowed for binary files ─────────────────────

class TestBinaryFileHandling(unittest.TestCase):
    """WRITE/CREATE over a binary file should fail, not silently corrupt it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def path(self, name):
        return os.path.join(self.tmp, name)

    def _write_binary(self, name):
        target = self.path(name)
        Path(target).write_bytes(b'\x89PNG\r\n\x1a\n\x00\x00\x00\x00\rIHDR\xff\xfe')
        return target

    def test_write_to_binary_file_fails(self):
        target = self._write_binary("binary.bin")
        result = execute(spec(op_write(target, "text content")))
        self.assertFalse(result.success)

    def test_write_to_binary_file_does_not_corrupt(self):
        target = self._write_binary("binary.bin")
        original = Path(target).read_bytes()
        execute(spec(op_write(target, "text content")))
        self.assertEqual(Path(target).read_bytes(), original)

    def test_write_to_binary_leaves_no_temp_files(self):
        target = self._write_binary("binary.bin")
        execute(spec(op_write(target, "text content")))
        leftovers = [f for f in os.listdir(self.tmp) if f.startswith(".fileops_")]
        self.assertEqual(leftovers, [])

    def test_delete_binary_file_succeeds(self):
        """DELETE on a binary file must still work — no diff needed."""
        target = self._write_binary("binary.bin")
        result = execute(spec(op_delete(target)))
        self.assertTrue(result.success)
        self.assertFalse(os.path.exists(target))

    def test_create_binary_content_is_not_supported(self):
        """CREATE with non-UTF-8 content is not in scope; UTF-8 text must work."""
        target = self.path("new.txt")
        result = execute(spec(op_create(target, "valid utf-8 content")))
        self.assertTrue(result.success)


# ── Bug #3: BatchSpec mutated by CLI / API layer ──────────────────────────────

class TestSpecNotMutated(unittest.TestCase):
    """The API _run helper must not mutate the caller's BatchSpec instance."""

    def test_api_force_dry_run_does_not_mutate_spec(self):
        from fileops.api.main import _run
        original = BatchSpec(
            operations=[FileOperation(type=OperationType.CREATE, path="x.txt", content="y")],
            dry_run=False,
        )
        _run(original, force_dry_run=True)
        self.assertFalse(original.dry_run)


# ── Bug #7: MOVE rollback coverage ────────────────────────────────────────────

class TestMoveWithExistingDestination(unittest.TestCase):
    """MOVE that overwrites an existing destination must back it up and restore on failure."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def path(self, name):
        return os.path.join(self.tmp, name)

    def test_move_overwrites_existing_destination(self):
        """Sanity check: successful MOVE replaces destination content."""
        src = self.path("src.txt")
        dst = self.path("dst.txt")
        Path(src).write_text("src content")
        Path(dst).write_text("dst original")
        result = execute(spec(op_move(src, dst)))
        self.assertTrue(result.success)
        self.assertFalse(os.path.exists(src))
        self.assertEqual(Path(dst).read_text(), "src content")

    def test_move_with_existing_dest_prepare_failure_leaves_both_untouched(self):
        """If batch fails in prepare (MOVE prepared OK, next op fails), both files untouched."""
        src = self.path("src.txt")
        dst = self.path("dst.txt")
        Path(src).write_text("src content")
        Path(dst).write_text("dst original")
        result = execute(spec(
            op_move(src, dst),
            op_delete(self.path("nope.txt")),  # fails in prepare
        ))
        self.assertFalse(result.success)
        # Nothing was committed — both files must be untouched
        self.assertTrue(os.path.exists(src))
        self.assertEqual(Path(src).read_text(), "src content")
        self.assertEqual(Path(dst).read_text(), "dst original")

    def test_move_no_temp_files_after_prepare_failure(self):
        src = self.path("src.txt")
        dst = self.path("dst.txt")
        Path(src).write_text("src content")
        Path(dst).write_text("dst original")
        execute(spec(
            op_move(src, dst),
            op_delete(self.path("nope.txt")),
        ))
        leftovers = [f for f in os.listdir(self.tmp) if f.startswith(".fileops_")]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
