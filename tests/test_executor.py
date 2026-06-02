"""
Tests for the atomic executor.
Uses stdlib unittest only — no external dependencies required.
"""

import os
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

from fileops.core import execute
from fileops.core.exceptions import RollbackFailedWarning
from fileops.core.models import BatchSpec, FileOperation, OperationType

# ── Helpers ───────────────────────────────────────────────────────────────────


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


class TestCreate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def test_create_new_file(self):
        target = self.path("hello.txt")
        result = execute(spec(op_create(target, "hello world")))
        self.assertTrue(result.success)
        self.assertFalse(result.rolled_back)
        self.assertEqual(Path(target).read_text(), "hello world")

    def test_create_generates_diff(self):
        target = self.path("new.txt")
        result = execute(spec(op_create(target, "line1\nline2\n")))
        self.assertTrue(result.success)
        self.assertIn("+line1", result.results[0].diff)

    def test_create_intermediate_dirs(self):
        target = self.path("a", "b", "c.txt")
        result = execute(spec(op_create(target, "nested")))
        self.assertTrue(result.success)
        self.assertTrue(os.path.exists(target))


class TestWrite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def path(self, name):
        return os.path.join(self.tmp, name)

    def test_write_overwrites_existing(self):
        target = self.path("file.txt")
        Path(target).write_text("old content")
        result = execute(spec(op_write(target, "new content")))
        self.assertTrue(result.success)
        self.assertEqual(Path(target).read_text(), "new content")

    def test_write_diff_shows_removed_and_added(self):
        target = self.path("file.txt")
        Path(target).write_text("before\n")
        result = execute(spec(op_write(target, "after\n")))
        diff = result.results[0].diff
        self.assertIn("-before", diff)
        self.assertIn("+after", diff)

    def test_write_no_diff_when_content_identical(self):
        target = self.path("file.txt")
        Path(target).write_text("same\n")
        result = execute(spec(op_write(target, "same\n")))
        self.assertTrue(result.success)
        self.assertIsNone(result.results[0].diff)


class TestDelete(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def path(self, name):
        return os.path.join(self.tmp, name)

    def test_delete_removes_file(self):
        target = self.path("gone.txt")
        Path(target).write_text("bye")
        result = execute(spec(op_delete(target)))
        self.assertTrue(result.success)
        self.assertFalse(os.path.exists(target))

    def test_delete_nonexistent_fails(self):
        result = execute(spec(op_delete(self.path("nope.txt"))))
        self.assertFalse(result.success)
        # Fails in prepare — nothing was committed, so rolled_back is False
        self.assertFalse(result.rolled_back)


class TestMove(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def test_move_renames_file(self):
        src = self.path("src.txt")
        dst = self.path("dst.txt")
        Path(src).write_text("contents")
        result = execute(spec(op_move(src, dst)))
        self.assertTrue(result.success)
        self.assertFalse(os.path.exists(src))
        self.assertEqual(Path(dst).read_text(), "contents")

    def test_move_creates_destination_dir(self):
        src = self.path("src.txt")
        dst = self.path("subdir", "dst.txt")
        Path(src).write_text("contents")
        result = execute(spec(op_move(src, dst)))
        self.assertTrue(result.success)
        self.assertTrue(os.path.exists(dst))


class TestBatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def path(self, name):
        return os.path.join(self.tmp, name)

    def test_batch_multiple_writes(self):
        a = self.path("a.txt")
        b = self.path("b.txt")
        Path(a).write_text("a_old")
        Path(b).write_text("b_old")
        result = execute(spec(op_write(a, "a_new"), op_write(b, "b_new")))
        self.assertTrue(result.success)
        self.assertEqual(result.operation_count, 2)
        self.assertEqual(Path(a).read_text(), "a_new")
        self.assertEqual(Path(b).read_text(), "b_new")

    def test_batch_mixed_operations(self):
        existing = self.path("existing.txt")
        new_file = self.path("new.txt")
        to_delete = self.path("delete_me.txt")
        Path(existing).write_text("original")
        Path(to_delete).write_text("gone soon")
        result = execute(spec(
            op_write(existing, "updated"),
            op_create(new_file, "created"),
            op_delete(to_delete),
        ))
        self.assertTrue(result.success)
        self.assertEqual(Path(existing).read_text(), "updated")
        self.assertEqual(Path(new_file).read_text(), "created")
        self.assertFalse(os.path.exists(to_delete))


class TestDryRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def path(self, name):
        return os.path.join(self.tmp, name)

    def test_dry_run_writes_nothing(self):
        target = self.path("untouched.txt")
        Path(target).write_text("original")
        execute(spec(op_write(target, "new content"), dry_run=True))
        self.assertEqual(Path(target).read_text(), "original")

    def test_dry_run_returns_diffs(self):
        target = self.path("file.txt")
        Path(target).write_text("old\n")
        result = execute(spec(op_write(target, "new\n"), dry_run=True))
        self.assertTrue(result.success)
        self.assertIn("-old", result.results[0].diff)
        self.assertIn("+new", result.results[0].diff)

    def test_dry_run_delete_does_not_remove_file(self):
        target = self.path("file.txt")
        Path(target).write_text("content\n")
        result = execute(spec(op_delete(target), dry_run=True))
        self.assertTrue(result.success)
        self.assertTrue(os.path.exists(target))


class TestRollback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def path(self, name):
        return os.path.join(self.tmp, name)

    def test_rollback_restores_modified_file(self):
        a = self.path("a.txt")
        Path(a).write_text("a_original")
        result = execute(spec(
            op_write(a, "a_new"),
            op_delete(self.path("nope.txt")),  # fails in prepare
        ))
        self.assertFalse(result.success)
        # Fails in prepare — nothing committed, so rolled_back is False
        self.assertFalse(result.rolled_back)
        self.assertEqual(Path(a).read_text(), "a_original")

    def test_rollback_removes_newly_created_file(self):
        new_file = self.path("new.txt")
        result = execute(spec(
            op_create(new_file, "new content"),
            op_delete(self.path("nope.txt")),  # fails in prepare
        ))
        self.assertFalse(result.success)
        # Fails in prepare — nothing committed, so rolled_back is False
        self.assertFalse(result.rolled_back)
        self.assertFalse(os.path.exists(new_file))


class TestCommitPhaseRollback(unittest.TestCase):
    """
    Failures DURING commit (not prepare) — the tool's core promise.

    These force os.replace to raise on the *second* commit, after the first has
    already succeeded, so the executor must reverse the committed op and report
    rolled_back=True. Prepare-phase tests can't reach this path.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def path(self, name):
        return os.path.join(self.tmp, name)

    def _fail_on_nth_replace(self, n):
        """Real os.replace, except the n-th call raises OSError."""
        real = os.replace
        state = {"calls": 0}

        def flaky(src, dst):
            state["calls"] += 1
            if state["calls"] == n:
                raise OSError("simulated commit failure")
            return real(src, dst)

        return flaky

    def test_commit_failure_restores_modified_file(self):
        a = self.path("a.txt")
        b = self.path("b.txt")
        Path(a).write_text("a_original")
        Path(b).write_text("b_original")

        # Commit order: replace(a) succeeds (#1), replace(b) fails (#2),
        # then rollback's replace(backup_a -> a) runs (#3, real).
        with mock.patch("fileops.core.executor.os.replace",
                        side_effect=self._fail_on_nth_replace(2)):
            result = execute(spec(op_write(a, "a_new"), op_write(b, "b_new")))

        self.assertFalse(result.success)
        self.assertTrue(result.rolled_back)
        self.assertIn("Commit failed", result.error or "")
        self.assertEqual(Path(a).read_text(), "a_original")  # committed then restored
        self.assertEqual(Path(b).read_text(), "b_original")  # commit never landed

    def test_commit_failure_removes_created_file(self):
        new_file = self.path("new.txt")
        existing = self.path("existing.txt")
        Path(existing).write_text("orig")

        # CREATE new commits (#1), WRITE existing fails (#2). Rollback must
        # unlink the newly created file (it has no backup to restore).
        with mock.patch("fileops.core.executor.os.replace",
                        side_effect=self._fail_on_nth_replace(2)):
            result = execute(spec(
                op_create(new_file, "created"),
                op_write(existing, "updated"),
            ))

        self.assertFalse(result.success)
        self.assertTrue(result.rolled_back)
        self.assertFalse(os.path.exists(new_file))      # created then removed
        self.assertEqual(Path(existing).read_text(), "orig")

    def test_commit_failure_leaves_no_temp_files(self):
        a = self.path("a.txt")
        b = self.path("b.txt")
        Path(a).write_text("a_original")
        Path(b).write_text("b_original")
        with mock.patch("fileops.core.executor.os.replace",
                        side_effect=self._fail_on_nth_replace(2)):
            execute(spec(op_write(a, "a_new"), op_write(b, "b_new")))
        leftovers = [f for f in os.listdir(self.tmp) if f.startswith(".fileops_")]
        self.assertEqual(leftovers, [])


class TestRollbackFailure(unittest.TestCase):
    """When rollback itself can't complete, the executor must warn, not crash."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def path(self, name):
        return os.path.join(self.tmp, name)

    def test_rollback_failure_emits_warning(self):
        a = self.path("a.txt")
        b = self.path("b.txt")
        Path(a).write_text("a_original")
        Path(b).write_text("b_original")

        real = os.replace
        state = {"calls": 0}

        def flaky(src, dst):
            state["calls"] += 1
            # #1 commit a succeeds; #2 commit b fails; #3 (rollback restore a) also fails.
            if state["calls"] >= 2:
                raise OSError("simulated failure")
            return real(src, dst)

        with mock.patch("fileops.core.executor.os.replace", side_effect=flaky):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                result = execute(spec(op_write(a, "a_new"), op_write(b, "b_new")))

        self.assertFalse(result.success)
        self.assertTrue(result.rolled_back)
        self.assertTrue(
            any(issubclass(w.category, RollbackFailedWarning) for w in caught),
            "expected a RollbackFailedWarning when rollback can't restore a file",
        )


@unittest.skipIf(getattr(os, "geteuid", lambda: 1)() == 0,
                 "running as root bypasses directory permissions")
class TestUnwritableDirectory(unittest.TestCase):
    """A write into a read-only directory must fail in prepare and change nothing."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_unwritable_dir_fails_cleanly(self):
        ro_dir = os.path.join(self.tmp, "readonly")
        os.makedirs(ro_dir)
        existing = os.path.join(ro_dir, "f.txt")
        Path(existing).write_text("orig")
        os.chmod(ro_dir, 0o500)  # r-x: can read, cannot create temp/backup files
        try:
            result = execute(spec(op_write(existing, "new content")))
            self.assertFalse(result.success)
            self.assertFalse(result.rolled_back)            # failed in prepare
            self.assertEqual(Path(existing).read_text(), "orig")
        finally:
            os.chmod(ro_dir, 0o700)  # restore so tearDown/cleanup can remove it


class TestTempFileCleanup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def path(self, name):
        return os.path.join(self.tmp, name)

    def leftover(self):
        return [f for f in os.listdir(self.tmp) if f.startswith(".fileops_")]

    def test_no_temp_files_after_success(self):
        target = self.path("file.txt")
        execute(spec(op_create(target, "content")))
        self.assertEqual(self.leftover(), [])

    def test_no_temp_files_after_rollback(self):
        new_file = self.path("new.txt")
        execute(spec(op_create(new_file, "x"), op_delete(self.path("nope.txt"))))
        self.assertEqual(self.leftover(), [])


if __name__ == "__main__":
    unittest.main()
