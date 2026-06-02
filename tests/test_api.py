"""
Tests for the fileops FastAPI app — endpoints, status codes, dry-run.
Uses FastAPI's TestClient (requires httpx, already a dev/runtime dep).
"""

import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from fileops.api.main import app


class TestAPI(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.tmp = tempfile.mkdtemp()

    def path(self, name):
        return os.path.join(self.tmp, name)

    def test_health(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"status": "ok"})

    def test_execute_writes_file(self):
        target = self.path("out.txt")
        r = self.client.post("/execute", json={
            "operations": [{"type": "create", "path": target, "content": "hello"}]
        })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["operation_count"], 1)
        self.assertEqual(Path(target).read_text(), "hello")

    def test_execute_failing_batch_is_200_with_success_false(self):
        # A logical failure (delete missing file) is a normal result, not an HTTP error.
        r = self.client.post("/execute", json={
            "operations": [{"type": "delete", "path": self.path("nope.txt")}]
        })
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["success"])

    def test_dry_run_does_not_write(self):
        target = self.path("out.txt")
        r = self.client.post("/dry-run", json={
            "operations": [{"type": "create", "path": target, "content": "hello"}]
        })
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["success"])
        self.assertFalse(os.path.exists(target))

    def test_invalid_spec_returns_422(self):
        r = self.client.post("/execute", json={
            "operations": [{"type": "teleport", "path": "x"}]
        })
        self.assertEqual(r.status_code, 422)

    def test_edit_op_via_api(self):
        target = self.path("cfg.py")
        Path(target).write_text("DEBUG = True\n")
        r = self.client.post("/execute", json={
            "operations": [{"type": "edit", "path": target,
                            "old_string": "True", "new_string": "False"}]
        })
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["success"])
        self.assertEqual(Path(target).read_text(), "DEBUG = False\n")


if __name__ == "__main__":
    unittest.main()
