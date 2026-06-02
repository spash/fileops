"""
Regression tests for the hardening pass (findings #1–#5 from the review).
Each class names the issue it covers and FAILS against the pre-fix code.
Uses stdlib unittest only — no external dependencies required.
"""

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pydantic import ValidationError

from fileops.core import execute, load_spec
from fileops.core.models import BatchSpec, FileOperation, OperationType


def spec(*ops, dry_run=False):
    return BatchSpec(operations=list(ops), dry_run=dry_run)

def op_write(path, content):
    return FileOperation(type=OperationType.WRITE, path=path, content=content)

def op_create(path, content):
    return FileOperation(type=OperationType.CREATE, path=path, content=content)

def op_edit(path, old, new, replace_all=False):
    return FileOperation(type=OperationType.EDIT, path=path,
                         old_string=old, new_string=new, replace_all=replace_all)

def op_insert(path, anchor, content, position):
    return FileOperation(type=OperationType.INSERT, path=path,
                         anchor=anchor, content=content, position=position)


# ── #1: WRITE/CREATE + EDIT/INSERT on the same file must compose ──────────────

class TestSameFileWriteThenEdit(unittest.TestCase):
    """A content op followed by an EDIT/INSERT on the same file must see the
    pending content, not stale disk text. Otherwise the first op is silently
    clobbered at commit while the batch reports success."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def path(self, name):
        return os.path.join(self.tmp, name)

    def test_write_then_edit_composes_without_losing_write(self):
        p = self.path("config.py")
        Path(p).write_text("VERSION = 1\n")
        # WRITE keeps "VERSION = 1" but adds a line; EDIT then bumps the version.
        r = execute(spec(
            op_write(p, "VERSION = 1\nSECURITY_PATCH = True\n"),
            op_edit(p, "VERSION = 1", "VERSION = 2"),
        ))
        self.assertTrue(r.success)
        self.assertEqual(Path(p).read_text(), "VERSION = 2\nSECURITY_PATCH = True\n")

    def test_write_to_new_file_then_edit(self):
        p = self.path("fresh.txt")  # does not exist on disk yet
        r = execute(spec(
            op_write(p, "alpha\n"),
            op_edit(p, "alpha", "beta"),
        ))
        self.assertTrue(r.success)
        self.assertEqual(Path(p).read_text(), "beta\n")

    def test_create_then_insert_composes(self):
        p = self.path("page.html")
        r = execute(spec(
            op_create(p, "<ul>\n</ul>\n"),
            op_insert(p, "</ul>", "  <li>x</li>\n", "before"),
        ))
        self.assertTrue(r.success)
        self.assertEqual(Path(p).read_text(), "<ul>\n  <li>x</li>\n</ul>\n")

    def test_write_then_failing_edit_rolls_back_to_original(self):
        p = self.path("config.py")
        Path(p).write_text("ORIGINAL\n")
        r = execute(spec(
            op_write(p, "REPLACED\n"),
            op_edit(p, "NOPE", "x"),  # fails in prepare
        ))
        self.assertFalse(r.success)
        self.assertEqual(Path(p).read_text(), "ORIGINAL\n")


# ── #2: permission bits must survive WRITE/EDIT/INSERT ────────────────────────

class TestPreservesFileMode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def path(self, name):
        return os.path.join(self.tmp, name)

    def mode(self, p):
        return stat.S_IMODE(os.stat(p).st_mode)

    def test_write_preserves_executable_bit(self):
        p = self.path("script.sh")
        Path(p).write_text("#!/bin/sh\necho old\n")
        os.chmod(p, 0o755)
        execute(spec(op_write(p, "#!/bin/sh\necho new\n")))
        self.assertEqual(self.mode(p), 0o755)

    def test_edit_preserves_mode(self):
        p = self.path("locked.conf")
        Path(p).write_text("KEY=old\n")
        os.chmod(p, 0o640)
        execute(spec(op_edit(p, "old", "new")))
        self.assertEqual(self.mode(p), 0o640)

    def test_insert_preserves_mode(self):
        p = self.path("list.txt")
        Path(p).write_text("a\nb\n")
        os.chmod(p, 0o600)
        execute(spec(op_insert(p, "a\n", "z\n", "after")))
        self.assertEqual(self.mode(p), 0o600)

    def test_created_file_honors_umask_not_0600(self):
        p = self.path("new.txt")
        cur = os.umask(0)
        os.umask(cur)
        execute(spec(op_create(p, "hi")))
        self.assertEqual(self.mode(p), 0o666 & ~cur)


# ── #3: BatchSpec must reject unknown top-level keys ──────────────────────────

class TestBatchSpecStrictKeys(unittest.TestCase):
    def test_unknown_batch_key_rejected_by_model(self):
        with self.assertRaises(ValidationError):
            BatchSpec.model_validate({
                "operations": [{"type": "create", "path": "x", "content": "y"}],
                "drz_run": True,  # typo of dry_run
            })

    def test_unknown_batch_key_rejected_by_loader(self):
        with self.assertRaises(ValueError):
            load_spec({
                "operations": [{"type": "create", "path": "x", "content": "y"}],
                "drz_run": True,
            })


# ── #4: makedirs side effects must be rolled back ─────────────────────────────

class TestMakedirsRolledBack(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def _fail_on_nth_replace(self, n):
        real = os.replace
        state = {"calls": 0}
        def flaky(src, dst):
            state["calls"] += 1
            if state["calls"] == n:
                raise OSError("simulated commit failure")
            return real(src, dst)
        return flaky

    def test_commit_rollback_removes_created_dirs(self):
        new = self.path("brand", "new", "deep.txt")  # CREATE makes brand/new/
        existing = self.path("existing.txt")
        Path(existing).write_text("orig")
        with mock.patch("fileops.core.executor.os.replace",
                        side_effect=self._fail_on_nth_replace(2)):
            r = execute(spec(
                op_create(new, "x"),
                op_write(existing, "updated"),
            ))
        self.assertTrue(r.rolled_back)
        self.assertFalse(os.path.exists(self.path("brand")))

    def test_dry_run_creates_no_dirs(self):
        new = self.path("ghost", "deep.txt")
        r = execute(spec(op_create(new, "x"), dry_run=True))
        self.assertTrue(r.success)
        self.assertFalse(os.path.exists(self.path("ghost")))

    def test_successful_batch_keeps_dirs(self):
        new = self.path("keep", "deep.txt")
        r = execute(spec(op_create(new, "x")))
        self.assertTrue(r.success)
        self.assertTrue(os.path.exists(new))


# ── #5: rolled-back ops must not be reported as successful ────────────────────

class TestHonestResultReporting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def path(self, name):
        return os.path.join(self.tmp, name)

    def _fail_on_nth_replace(self, n):
        real = os.replace
        state = {"calls": 0}
        def flaky(src, dst):
            state["calls"] += 1
            if state["calls"] == n:
                raise OSError("simulated commit failure")
            return real(src, dst)
        return flaky

    def test_no_op_reports_success_after_rollback(self):
        a = self.path("a.txt")
        b = self.path("b.txt")
        Path(a).write_text("a_orig")
        Path(b).write_text("b_orig")
        with mock.patch("fileops.core.executor.os.replace",
                        side_effect=self._fail_on_nth_replace(2)):
            r = execute(spec(op_write(a, "a_new"), op_write(b, "b_new")))
        self.assertFalse(r.success)
        self.assertTrue(r.rolled_back)
        self.assertFalse(any(res.success for res in r.results))
        self.assertEqual(r.success_count, 0)

    def test_failing_op_carries_its_error(self):
        a = self.path("a.txt")
        b = self.path("b.txt")
        Path(a).write_text("a_orig")
        Path(b).write_text("b_orig")
        with mock.patch("fileops.core.executor.os.replace",
                        side_effect=self._fail_on_nth_replace(2)):
            r = execute(spec(op_write(a, "a_new"), op_write(b, "b_new")))
        # results cover all ops; the second (failing) one names the failure,
        # the first reports it was rolled back.
        self.assertEqual(len(r.results), 2)
        self.assertIn("rolled back", r.results[0].error or "")
        self.assertIn("simulated commit failure", r.results[1].error or "")


if __name__ == "__main__":
    unittest.main()
