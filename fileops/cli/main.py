"""
fileops CLI

Usage:
  fileops run spec.json
  fileops run spec.yaml --dry-run
  fileops run spec.json --dry-run --diff
  echo '{"operations":[...]}' | fileops run -
"""

import json
import sys

import click

from fileops.core import execute, load_spec
from fileops.core.models import BatchResult


@click.group()
@click.version_option()
def cli() -> None:
    """Atomic batch file operations for AI agent workflows."""


@cli.command()
@click.argument("spec_file", metavar="SPEC", default="-", type=click.Path(allow_dash=True))
@click.option("--dry-run", is_flag=True, default=False, help="Validate and diff; write nothing.")
@click.option("--diff", "show_diff", is_flag=True, default=False, help="Print unified diffs.")
@click.option(
    "--json", "output_json", is_flag=True, default=False, help="Machine-readable JSON output."
)
def run(spec_file: str, dry_run: bool, show_diff: bool, output_json: bool) -> None:
    """
    Execute operations defined in SPEC (JSON or YAML).

    Pass '-' to read from stdin.

    \b
    Examples:
      fileops run changes.json
      fileops run changes.yaml --dry-run --diff
      cat changes.json | fileops run -
    """
    # Load spec
    try:
        if spec_file == "-":
            source = click.get_text_stream("stdin").read()
            spec = load_spec(source)
        else:
            spec = load_spec(spec_file)
    except (ValueError, FileNotFoundError) as exc:
        _err(f"Invalid spec: {exc}", output_json)
        sys.exit(1)

    # Apply CLI flag — CLI --dry-run overrides spec value
    if dry_run:
        spec = spec.model_copy(update={"dry_run": True})

    # Execute
    result = execute(spec)

    if output_json:
        _print_json(result)
    else:
        _print_human(result, show_diff)

    sys.exit(0 if result.success else 1)


# ── Output formatters ─────────────────────────────────────────────────────────


def _print_human(result: BatchResult, show_diff: bool) -> None:
    status = (
        click.style("✓ done", fg="green") if result.success else click.style("✗ failed", fg="red")
    )

    if result.rolled_back:
        status += click.style("  (rolled back)", fg="yellow")

    click.echo(f"\n{status}  {result.success_count}/{result.operation_count} operations\n")

    for r in result.results:
        icon = click.style("✓", fg="green") if r.success else click.style("✗", fg="red")
        click.echo(f"  {icon}  {r.operation.summary()}")

        if r.error:
            click.echo(f"     {click.style(r.error, fg='red')}")

        if show_diff and r.diff:
            click.echo()
            _print_diff(r.diff)

    if result.error:
        click.echo(f"\n{click.style('Error:', fg='red')} {result.error}")

    click.echo()


def _print_diff(diff: str) -> None:
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            click.echo(click.style(line, fg="white", bold=True))
        elif line.startswith("+"):
            click.echo(click.style(line, fg="green"))
        elif line.startswith("-"):
            click.echo(click.style(line, fg="red"))
        elif line.startswith("@@"):
            click.echo(click.style(line, fg="cyan"))
        else:
            click.echo(line)


def _print_json(result: BatchResult) -> None:
    click.echo(result.model_dump_json(indent=2))


def _err(message: str, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps({"success": False, "error": message}))
    else:
        click.echo(click.style(f"Error: {message}", fg="red"), err=True)


def main() -> None:
    cli()
