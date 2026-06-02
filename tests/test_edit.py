"""
Tests for the EDIT and INSERT operations.
Uses stdlib unittest only — no external dependencies required.
"""

import os
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from fileops.core import execute
from fileops.core.models import BatchSpec, FileOperation, OperationType

# ── Helpers ───────────────────────────────────────────────────────────────────


def spec(*ops, dry_run=False):
    return BatchSpec(operations=list(ops), dry_run=dry_run)

def op_write(path, content):
    return FileOperation(type=OperationType.WRITE, path=path, content=content)

def op_delete(path):
    return FileOperation(type=OperationType.DELETE, path=path)

def op_edit(path, old, new, replace_all=False):
    return FileOperation(
        type=OperationType.EDIT, path=path,
        old_string=old, new_string=new, replace_all=replace_all,
    )

def op_insert(path, anchor, content, position):
    return FileOperation(
        type=OperationType.INSERT, path=path,
        anchor=anchor, content=content, position=position,
    )


class TestEdit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def path(self, name):
        return os.path.join(self.tmp, name)

    def write(self, name, text):
        target = self.path(name)
        Path(target).write_text(text)
        return target

    def test_edit_single_match(self):
        target = self.write("f.txt", "hello world\n")
        result = execute(spec(op_edit(target, "world", "there")))
        self.assertTrue(result.success)
        self.assertEqual(Path(target).read_text(), "hello there\n")

    def test_edit_produces_diff(self):
        target = self.write("f.txt", "a\nworld\nb\n")
        result = execute(spec(op_edit(target, "world", "there")))
        diff = result.results[0].diff
        self.assertIn("-world", diff)
        self.assertIn("+there", diff)

    def test_edit_replace_all(self):
        target = self.write("f.txt", "x x x\n")
        result = execute(spec(op_edit(target, "x", "y", replace_all=True)))
        self.assertTrue(result.success)
        self.assertEqual(Path(target).read_text(), "y y y\n")

    def test_edit_ambiguous_fails_and_leaves_file_unchanged(self):
        original = "x x x\n"
        target = self.write("f.txt", original)
        result = execute(spec(op_edit(target, "x", "y")))  # replace_all=False
        self.assertFalse(result.success)
        self.assertFalse(result.rolled_back)  # failed in prepare
        self.assertEqual(Path(target).read_text(), original)

    def test_edit_not_found_fails_and_leaves_file_unchanged(self):
        original = "hello\n"
        target = self.write("f.txt", original)
        result = execute(spec(op_edit(target, "missing", "x")))
        self.assertFalse(result.success)
        self.assertEqual(Path(target).read_text(), original)

    def test_edit_noop_rejected_at_validation(self):
        with self.assertRaises(ValidationError):
            op_edit(self.path("f.txt"), "same", "same")

    def test_edit_nonexistent_path_fails(self):
        result = execute(spec(op_edit(self.path("nope.txt"), "a", "b")))
        self.assertFalse(result.success)
        self.assertFalse(result.rolled_back)


class TestInsert(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def path(self, name):
        return os.path.join(self.tmp, name)

    def write(self, name, text):
        target = self.path(name)
        Path(target).write_text(text)
        return target

    def test_insert_after_anchor(self):
        target = self.write("nav.html", "<a>one</a>\n</div>\n")
        result = execute(spec(op_insert(target, "<a>one</a>\n", "<a>two</a>\n", "after")))
        self.assertTrue(result.success)
        self.assertEqual(Path(target).read_text(), "<a>one</a>\n<a>two</a>\n</div>\n")

    def test_insert_before_anchor(self):
        target = self.write("nav.html", "</div>\n")
        result = execute(spec(op_insert(target, "</div>", "<a>new</a>\n", "before")))
        self.assertTrue(result.success)
        self.assertEqual(Path(target).read_text(), "<a>new</a>\n</div>\n")

    def test_insert_anchor_not_found_fails(self):
        original = "nothing here\n"
        target = self.write("f.txt", original)
        result = execute(spec(op_insert(target, "</div>", "x", "after")))
        self.assertFalse(result.success)
        self.assertEqual(Path(target).read_text(), original)

    def test_insert_ambiguous_anchor_fails(self):
        original = "<li>\n<li>\n"
        target = self.write("f.txt", original)
        result = execute(spec(op_insert(target, "<li>", "x", "after")))
        self.assertFalse(result.success)
        self.assertEqual(Path(target).read_text(), original)

    def test_insert_requires_valid_position(self):
        with self.assertRaises(ValidationError):
            op_insert(self.path("f.txt"), "anchor", "x", "sideways")


class TestSameFileComposition(unittest.TestCase):
    """Multiple EDIT/INSERT ops on one file must compose, not clobber."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def path(self, name):
        return os.path.join(self.tmp, name)

    def test_two_independent_inserts_on_one_file_both_apply(self):
        # The plan's motivating case: a nav link AND a footer line into one file.
        target = self.path("page.html")
        Path(target).write_text("<a>Home</a>\n<li>About</li>\n")
        result = execute(spec(
            op_insert(target, "<a>Home</a>\n", "<a>Work</a>\n", "after"),
            op_insert(target, "<li>About</li>\n", "<li>Contact</li>\n", "after"),
        ))
        self.assertTrue(result.success)
        self.assertEqual(
            Path(target).read_text(),
            "<a>Home</a>\n<a>Work</a>\n<li>About</li>\n<li>Contact</li>\n",
        )

    def test_edit_then_edit_chains(self):
        # Second edit matches text produced by the first.
        target = self.path("f.txt")
        Path(target).write_text("alpha\n")
        result = execute(spec(
            op_edit(target, "alpha", "beta"),
            op_edit(target, "beta", "gamma"),
        ))
        self.assertTrue(result.success)
        self.assertEqual(Path(target).read_text(), "gamma\n")

    def test_one_failing_edit_in_same_file_chain_rolls_back(self):
        target = self.path("f.txt")
        Path(target).write_text("alpha\n")
        result = execute(spec(
            op_edit(target, "alpha", "beta"),
            op_edit(target, "NOPE", "gamma"),  # fails — original must survive
        ))
        self.assertFalse(result.success)
        self.assertEqual(Path(target).read_text(), "alpha\n")


class TestBinaryFiles(unittest.TestCase):
    """EDIT/INSERT on a non-UTF-8 file must fail cleanly, not corrupt it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def path(self, name):
        return os.path.join(self.tmp, name)

    def _write_binary(self, name):
        target = self.path(name)
        Path(target).write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\x00\rIHDR\xff\xfe")
        return target

    def test_edit_binary_fails_and_does_not_corrupt(self):
        target = self._write_binary("img.bin")
        original = Path(target).read_bytes()
        result = execute(spec(op_edit(target, "x", "y")))
        self.assertFalse(result.success)
        self.assertEqual(Path(target).read_bytes(), original)

    def test_insert_binary_fails_and_does_not_corrupt(self):
        target = self._write_binary("img.bin")
        original = Path(target).read_bytes()
        result = execute(spec(op_insert(target, "x", "y", "after")))
        self.assertFalse(result.success)
        self.assertEqual(Path(target).read_bytes(), original)

    def test_binary_edit_leaves_no_temp_files(self):
        target = self._write_binary("img.bin")
        execute(spec(op_edit(target, "x", "y")))
        leftovers = [f for f in os.listdir(self.tmp) if f.startswith(".fileops_")]
        self.assertEqual(leftovers, [])


class TestMixedBatchRollback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def path(self, name):
        return os.path.join(self.tmp, name)

    def test_failing_edit_rolls_back_whole_batch(self):
        # A WRITE + EDIT + DELETE batch where the EDIT can't match → nothing commits.
        new_file = self.path("created.txt")
        edit_target = self.path("edit.txt")
        delete_target = self.path("delete.txt")
        Path(edit_target).write_text("original\n")
        Path(delete_target).write_text("doomed\n")

        result = execute(spec(
            op_write(new_file, "fresh\n"),
            op_edit(edit_target, "MISSING", "x"),   # fails in prepare
            op_delete(delete_target),
        ))

        self.assertFalse(result.success)
        # Every file is exactly as it started — none of the batch applied.
        self.assertFalse(os.path.exists(new_file))
        self.assertEqual(Path(edit_target).read_text(), "original\n")
        self.assertTrue(os.path.exists(delete_target))

    def test_successful_mixed_batch_applies_all(self):
        new_file = self.path("created.txt")
        edit_target = self.path("edit.txt")
        delete_target = self.path("delete.txt")
        Path(edit_target).write_text("original\n")
        Path(delete_target).write_text("doomed\n")

        result = execute(spec(
            op_write(new_file, "fresh\n"),
            op_edit(edit_target, "original", "changed"),
            op_delete(delete_target),
        ))

        self.assertTrue(result.success)
        self.assertEqual(Path(new_file).read_text(), "fresh\n")
        self.assertEqual(Path(edit_target).read_text(), "changed\n")
        self.assertFalse(os.path.exists(delete_target))


if __name__ == "__main__":
    unittest.main()
