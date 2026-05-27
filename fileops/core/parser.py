"""
Load a BatchSpec from JSON or YAML.

Supports:
  - File path (str)
  - File-like object
  - Raw dict (already parsed)

Format is auto-detected from file extension or by attempting JSON first.
"""

from __future__ import annotations

import json
import os
from typing import IO, Any, Union

from pydantic import ValidationError

from .models import BatchSpec

try:
    import yaml

    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False


def load_spec(source: Union[str, IO[str], dict]) -> BatchSpec:
    """
    Parse a BatchSpec from a file path, file-like object, or raw dict.

    Args:
        source: Path to a .json or .yaml/.yml file, an open file object,
                or an already-parsed dict.

    Returns:
        BatchSpec ready for execution.

    Raises:
        ValueError: If the spec is malformed or contains unknown fields.
        FileNotFoundError: If a file path is given and doesn't exist.
    """
    if isinstance(source, dict):
        return _parse_dict(source)

    if isinstance(source, str) and os.path.exists(source):
        return _load_file(source)

    if isinstance(source, str):
        # Treat as raw content — try JSON then YAML
        return _parse_content(source, hint=None)

    # File-like object
    content = source.read()
    name = getattr(source, "name", "")
    hint = _ext(name)
    return _parse_content(content, hint=hint)


def _load_file(path: str) -> BatchSpec:
    hint = _ext(path)
    if hint == "yaml" and not _YAML_AVAILABLE:
        raise ImportError("PyYAML is required to load .yaml/.yml files: pip install pyyaml")
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    return _parse_content(content, hint=hint)


def _parse_content(content: str, hint: str | None) -> BatchSpec:
    if hint == "yaml":
        data = _load_yaml(content)
    elif hint == "json":
        data = json.loads(content)
    else:
        # Auto-detect: try JSON first, fall back to YAML
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            if _YAML_AVAILABLE:
                data = _load_yaml(content)
            else:
                raise ValueError(
                    "Content is not valid JSON. Install PyYAML to enable YAML support."
                )
    return _parse_dict(data)


def _load_yaml(content: str) -> Any:
    if not _YAML_AVAILABLE:
        raise ImportError("PyYAML is required: pip install pyyaml")
    return yaml.safe_load(content)


def _parse_dict(data: dict) -> BatchSpec:
    if not isinstance(data, dict):
        raise ValueError(f"Spec must be a mapping, got {type(data).__name__}")
    try:
        return BatchSpec.model_validate(data)
    except ValidationError as e:
        raise ValueError(f"Invalid spec format: {e}")


def _ext(name: str) -> str | None:
    _, ext = os.path.splitext(name.lower())
    if ext in (".yaml", ".yml"):
        return "yaml"
    if ext == ".json":
        return "json"
    return None
