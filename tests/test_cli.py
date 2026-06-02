"""
Tests for the fileops CLI — click command wiring, flags, stdin, exit codes.
Uses click's CliRunner with an isolated filesystem (relative paths are safe).
"""

import json
import unittest
from pathlib import Path

from click.testing import CliRunner

from fileops.cli.main import cli


def spec_json(ops, **extra):
    return json.dumps({"operations": ops, **extra})


class TestCLIRun(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_run_executes_and_writes(self):
        with self.runner.isolated_filesystem():
            Path("spec.json").write_text(
                spec_json([{"type": "create", "path": "out.txt", "content": "hello"}])
            )
            result = self.runner.invoke(cli, ["run", "spec.json"])
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(Path("out.txt").read_text(), "hello")
            self.assertIn("done", result.output)

    def test_dry_run_writes_nothing(self):
        with self.runner.isolated_filesystem():
            Path("spec.json").write_text(
                spec_json([{"type": "create", "path": "out.txt", "content": "hello"}])
            )
            result = self.runner.invoke(cli, ["run", "spec.json", "--dry-run"])
            self.assertEqual(result.exit_code, 0)
            self.assertFalse(Path("out.txt").exists())

    def test_json_output_is_machine_readable(self):
        with self.runner.isolated_filesystem():
            Path("spec.json").write_text(
                spec_json([{"type": "create", "path": "out.txt", "content": "x"}])
            )
            result = self.runner.invoke(cli, ["run", "spec.json", "--json"])
            self.assertEqual(result.exit_code, 0)
            data = json.loads(result.output)
            self.assertTrue(data["success"])
            self.assertEqual(data["operation_count"], 1)

    def test_reads_spec_from_stdin(self):
        with self.runner.isolated_filesystem():
            spec = spec_json([{"type": "create", "path": "out.txt", "content": "hi"}])
            result = self.runner.invoke(cli, ["run", "-"], input=spec)
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(Path("out.txt").read_text(), "hi")

    def test_failing_batch_exits_nonzero(self):
        with self.runner.isolated_filesystem():
            Path("spec.json").write_text(
                spec_json([{"type": "delete", "path": "nope.txt"}])
            )
            result = self.runner.invoke(cli, ["run", "spec.json"])
            self.assertEqual(result.exit_code, 1)
            self.assertIn("failed", result.output)

    def test_invalid_spec_exits_nonzero(self):
        with self.runner.isolated_filesystem():
            Path("spec.json").write_text("{ not valid json")
            result = self.runner.invoke(cli, ["run", "spec.json"])
            self.assertEqual(result.exit_code, 1)

    def test_diff_flag_shows_changes(self):
        with self.runner.isolated_filesystem():
            Path("out.txt").write_text("old\n")
            Path("spec.json").write_text(
                spec_json([{"type": "write", "path": "out.txt", "content": "new\n"}])
            )
            result = self.runner.invoke(cli, ["run", "spec.json", "--diff"])
            self.assertEqual(result.exit_code, 0)
            self.assertIn("new", result.output)

    def test_edit_op_via_cli(self):
        with self.runner.isolated_filesystem():
            Path("cfg.py").write_text("DEBUG = True\n")
            Path("spec.json").write_text(spec_json([
                {"type": "edit", "path": "cfg.py",
                 "old_string": "True", "new_string": "False"},
            ]))
            result = self.runner.invoke(cli, ["run", "spec.json"])
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(Path("cfg.py").read_text(), "DEBUG = False\n")


if __name__ == "__main__":
    unittest.main()
