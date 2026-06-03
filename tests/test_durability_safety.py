"""
Tests for the durability & safety pass:
  - fsync of data (before rename) and directory (after rename)
  - refusal to operate on symlinks
  - API path confinement via FILEOPS_ROOT
Uses stdlib unittest only (plus FastAPI's TestClient for the API tests).
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fileops.core import execute
from fileops.core.models import BatchSpec, FileOperation, OperationType


def spec(*ops, dry_run=False):
    return BatchSpec(operations=list(ops), dry_run=dry_run)

def op_write(path, content):
    return FileOperation(type=OperationType.WRITE, path=path, content=content)

def op_create(path, content):
    return FileOperation(type=OperationType.CREATE, path=path, content=content)

def op_edit(path, old, new):
    return FileOperation(type=OperationType.EDIT, path=path, old_string=old, new_string=new)

def op_delete(path):
    return FileOperation(type=OperationType.DELETE, path=path)

def op_move(path, dest):
    return FileOperation(type=OperationType.MOVE, path=path, destination=dest)


# ── Durability: data + directory fsync ────────────────────────────────────────

class TestFsyncDurability(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def path(self, name):
        return os.path.join(self.tmp, name)

    def test_create_fsyncs_data_and_directory(self):
        # One CREATE should fsync at least twice: the file's data before the
        # rename, and the parent directory after it.
        with mock.patch("fileops.core.executor.os.fsync") as fsync:
            r = execute(spec(op_create(self.path("f.txt"), "hi")))
        self.assertTrue(r.success)
        self.assertGreaterEqual(fsync.call_count, 2)

    def test_delete_fsyncs_directory(self):
        p = self.path("gone.txt")
        Path(p).write_text("bye")
        with mock.patch("fileops.core.executor.os.fsync") as fsync:
            r = execute(spec(op_delete(p)))
        self.assertTrue(r.success)
        self.assertGreaterEqual(fsync.call_count, 1)

    def test_fsync_runs_for_real_without_error(self):
        # Sanity: real fsync path works on this filesystem (no mock).
        p = self.path("real.txt")
        r = execute(spec(op_create(p, "content")))
        self.assertTrue(r.success)
        self.assertEqual(Path(p).read_text(), "content")


# ── Safety: refuse to operate on symlinks ─────────────────────────────────────

class TestSymlinkRefusal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def path(self, name):
        return os.path.join(self.tmp, name)

    def _link_to_real(self, link_name="link.txt", real_name="real.txt", text="original\n"):
        real = self.path(real_name)
        Path(real).write_text(text)
        link = self.path(link_name)
        os.symlink(real, link)
        return link, real

    def test_write_to_symlink_refused_link_and_target_intact(self):
        link, real = self._link_to_real()
        r = execute(spec(op_write(link, "new\n")))
        self.assertFalse(r.success)
        self.assertTrue(os.path.islink(link))
        self.assertEqual(Path(real).read_text(), "original\n")

    def test_edit_symlink_refused(self):
        link, real = self._link_to_real()
        r = execute(spec(op_edit(link, "original", "changed")))
        self.assertFalse(r.success)
        self.assertTrue(os.path.islink(link))
        self.assertEqual(Path(real).read_text(), "original\n")

    def test_delete_symlink_refused(self):
        link, real = self._link_to_real()
        r = execute(spec(op_delete(link)))
        self.assertFalse(r.success)
        self.assertTrue(os.path.islink(link))
        self.assertTrue(os.path.exists(real))

    def test_move_symlink_source_refused(self):
        link, real = self._link_to_real()
        r = execute(spec(op_move(link, self.path("moved.txt"))))
        self.assertFalse(r.success)
        self.assertTrue(os.path.islink(link))

    def test_move_onto_symlink_destination_refused(self):
        link, real = self._link_to_real()
        src = self.path("src.txt")
        Path(src).write_text("src\n")
        r = execute(spec(op_move(src, link)))
        self.assertFalse(r.success)
        self.assertTrue(os.path.islink(link))
        self.assertEqual(Path(src).read_text(), "src\n")  # source untouched

    def test_create_on_broken_symlink_refused(self):
        broken = self.path("broken")
        os.symlink(self.path("does_not_exist"), broken)
        r = execute(spec(op_create(broken, "x")))
        self.assertFalse(r.success)
        self.assertTrue(os.path.islink(broken))


# ── API: path confinement via FILEOPS_ROOT ────────────────────────────────────

class TestAPIConfinement(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        from fileops.api.main import app
        self.client = TestClient(app)
        self.root = tempfile.mkdtemp()
        self._saved = os.environ.get("FILEOPS_ROOT")
        os.environ["FILEOPS_ROOT"] = self.root

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("FILEOPS_ROOT", None)
        else:
            os.environ["FILEOPS_ROOT"] = self._saved

    def test_path_inside_root_allowed(self):
        target = os.path.join(self.root, "ok.txt")
        r = self.client.post("/execute", json={
            "operations": [{"type": "create", "path": target, "content": "hi"}]
        })
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["success"])

    def test_absolute_path_outside_root_rejected_403(self):
        outside = os.path.join(tempfile.mkdtemp(), "escape.txt")
        r = self.client.post("/execute", json={
            "operations": [{"type": "create", "path": outside, "content": "x"}]
        })
        self.assertEqual(r.status_code, 403)
        self.assertFalse(os.path.exists(outside))

    def test_dotdot_escape_rejected_403(self):
        escape = os.path.join(self.root, "..", "escape.txt")
        r = self.client.post("/execute", json={
            "operations": [{"type": "create", "path": escape, "content": "x"}]
        })
        self.assertEqual(r.status_code, 403)

    def test_move_destination_outside_root_rejected(self):
        src = os.path.join(self.root, "src.txt")
        Path(src).write_text("data")
        outside = os.path.join(tempfile.mkdtemp(), "dst.txt")
        r = self.client.post("/execute", json={
            "operations": [{"type": "move", "path": src, "destination": outside}]
        })
        self.assertEqual(r.status_code, 403)
        self.assertTrue(os.path.exists(src))  # nothing moved

    def test_no_root_set_means_no_confinement(self):
        # Backwards-compatible: without FILEOPS_ROOT, any path is allowed.
        os.environ.pop("FILEOPS_ROOT", None)
        outside = os.path.join(tempfile.mkdtemp(), "free.txt")
        r = self.client.post("/execute", json={
            "operations": [{"type": "create", "path": outside, "content": "x"}]
        })
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["success"])


if __name__ == "__main__":
    unittest.main()
