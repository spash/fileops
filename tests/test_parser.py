"""Tests for spec parsing — JSON, YAML, dict input, and validation errors."""

import json
import os
import tempfile
import unittest

from fileops.core.models import OperationType
from fileops.core.parser import load_spec

VALID_JSON = json.dumps({
    "description": "test batch",
    "operations": [
        {"type": "write", "path": "src/foo.py", "content": "print('hi')"},
        {"type": "delete", "path": "src/old.py"},
        {"type": "move", "path": "src/a.py", "destination": "src/b.py"},
    ],
})

VALID_DICT = {
    "operations": [{"type": "create", "path": "out.txt", "content": "hello"}]
}


class TestLoadFromDict(unittest.TestCase):
    def test_basic(self):
        spec = load_spec(VALID_DICT)
        self.assertEqual(spec.operation_count, 1)
        self.assertEqual(spec.operations[0].type, OperationType.CREATE)

    def test_dry_run_flag(self):
        raw = {"dry_run": True, "operations": [{"type": "create", "path": "x", "content": "y"}]}
        spec = load_spec(raw)
        self.assertTrue(spec.dry_run)

    def test_dry_run_defaults_false(self):
        spec = load_spec(VALID_DICT)
        self.assertFalse(spec.dry_run)

    def test_move_parsed(self):
        raw = {"operations": [{"type": "move", "path": "old.py", "destination": "new.py"}]}
        spec = load_spec(raw)
        op = spec.operations[0]
        self.assertEqual(op.type, OperationType.MOVE)
        self.assertEqual(op.destination, "new.py")

    def test_edit_parsed(self):
        raw = {"operations": [{
            "type": "edit", "path": "x.py",
            "old_string": "a", "new_string": "b", "replace_all": True,
        }]}
        op = load_spec(raw).operations[0]
        self.assertEqual(op.type, OperationType.EDIT)
        self.assertEqual(op.old_string, "a")
        self.assertEqual(op.new_string, "b")
        self.assertTrue(op.replace_all)

    def test_insert_parsed(self):
        raw = {"operations": [{
            "type": "insert", "path": "x.html",
            "anchor": "</div>", "position": "before", "content": "<a/>",
        }]}
        op = load_spec(raw).operations[0]
        self.assertEqual(op.type, OperationType.INSERT)
        self.assertEqual(op.position, "before")


class TestLoadFromJSON(unittest.TestCase):
    def test_from_string(self):
        spec = load_spec(VALID_JSON)
        self.assertEqual(spec.operation_count, 3)
        self.assertEqual(spec.description, "test batch")

    def test_from_file(self):
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            f.write(VALID_JSON)
            fname = f.name
        try:
            spec = load_spec(fname)
            self.assertEqual(spec.operation_count, 3)
        finally:
            os.unlink(fname)


class TestValidationErrors(unittest.TestCase):
    def test_missing_operations_key(self):
        with self.assertRaises(ValueError):
            load_spec({"description": "no ops"})

    def test_empty_operations_list(self):
        with self.assertRaises(ValueError):
            load_spec({"operations": []})

    def test_missing_path(self):
        with self.assertRaises(ValueError):
            load_spec({"operations": [{"type": "create", "content": "x"}]})

    def test_missing_type(self):
        with self.assertRaises(ValueError):
            load_spec({"operations": [{"path": "x.txt", "content": "x"}]})

    def test_write_missing_content(self):
        with self.assertRaises(ValueError):
            load_spec({"operations": [{"type": "write", "path": "x.txt"}]})

    def test_move_missing_destination(self):
        with self.assertRaises(ValueError):
            load_spec({"operations": [{"type": "move", "path": "x.txt"}]})

    def test_edit_missing_strings(self):
        with self.assertRaises(ValueError):
            load_spec({"operations": [{"type": "edit", "path": "x.txt", "old_string": "a"}]})

    def test_edit_noop_rejected(self):
        with self.assertRaises(ValueError):
            load_spec({"operations": [
                {"type": "edit", "path": "x.txt", "old_string": "a", "new_string": "a"}
            ]})

    def test_insert_bad_position(self):
        with self.assertRaises(ValueError):
            load_spec({"operations": [
                {"type": "insert", "path": "x.txt", "anchor": "a",
                 "position": "sideways", "content": "c"}
            ]})

    def test_unknown_fields_rejected(self):
        with self.assertRaises(ValueError):
            load_spec({"operations": [
                {"type": "create", "path": "x.txt", "content": "y", "bogus": True}
            ]})

    def test_invalid_operation_type(self):
        with self.assertRaises(ValueError):
            load_spec({"operations": [{"type": "teleport", "path": "x.txt"}]})


if __name__ == "__main__":
    unittest.main()
