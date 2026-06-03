"""
Tests for binary content via content_encoding="base64" on CREATE/WRITE.
Uses stdlib unittest only.
"""

import base64
import os
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from fileops.core import execute
from fileops.core.models import BatchSpec, FileOperation, OperationType

PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\xff\xfe\x00"


def spec(*ops, dry_run=False):
    return BatchSpec(operations=list(ops), dry_run=dry_run)

def op_create_b64(path, data: bytes):
    return FileOperation(type=OperationType.CREATE, path=path,
                         content=base64.b64encode(data).decode(), content_encoding="base64")

def op_write_b64(path, data: bytes):
    return FileOperation(type=OperationType.WRITE, path=path,
                         content=base64.b64encode(data).decode(), content_encoding="base64")


class TestBinaryContent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def path(self, name):
        return os.path.join(self.tmp, name)

    def test_create_binary_file_roundtrips(self):
        p = self.path("img.png")
        r = execute(spec(op_create_b64(p, PNG_BYTES)))
        self.assertTrue(r.success)
        self.assertEqual(Path(p).read_bytes(), PNG_BYTES)

    def test_write_binary_over_existing(self):
        p = self.path("data.bin")
        Path(p).write_bytes(b"old bytes")
        r = execute(spec(op_write_b64(p, PNG_BYTES)))
        self.assertTrue(r.success)
        self.assertEqual(Path(p).read_bytes(), PNG_BYTES)

    def test_write_binary_over_existing_binary_then_rolls_back(self):
        # base64 WRITE over a non-UTF-8 file must work (no text diff attempted)
        # and still roll back cleanly if a later op fails.
        p = self.path("data.bin")
        Path(p).write_bytes(b"\xff\xfeoriginal")
        r = execute(spec(
            op_write_b64(p, PNG_BYTES),
            FileOperation(type=OperationType.DELETE, path=self.path("nope")),  # fails
        ))
        self.assertFalse(r.success)
        self.assertEqual(Path(p).read_bytes(), b"\xff\xfeoriginal")

    def test_invalid_base64_fails_cleanly(self):
        p = self.path("bad.bin")
        op = FileOperation(type=OperationType.CREATE, path=p,
                           content="not!valid!base64!!!", content_encoding="base64")
        r = execute(spec(op))
        self.assertFalse(r.success)
        self.assertFalse(os.path.exists(p))

    def test_dry_run_binary_writes_nothing(self):
        p = self.path("img.png")
        r = execute(spec(op_create_b64(p, PNG_BYTES), dry_run=True))
        self.assertTrue(r.success)
        self.assertFalse(os.path.exists(p))
        self.assertIn("binary file", r.results[0].diff or "")

    def test_content_encoding_rejected_on_non_write_ops(self):
        with self.assertRaises(ValidationError):
            FileOperation(type=OperationType.DELETE, path="x", content_encoding="base64")

    def test_invalid_encoding_value_rejected(self):
        with self.assertRaises(ValidationError):
            FileOperation(type=OperationType.CREATE, path="x", content="y",
                          content_encoding="rot13")


if __name__ == "__main__":
    unittest.main()
