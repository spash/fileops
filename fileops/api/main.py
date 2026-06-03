"""
fileops API server

POST /execute     — run a BatchSpec, return BatchResult
POST /dry-run     — same but forces dry_run=True; writes nothing

Start with: uvicorn fileops.api.main:app --reload

Environment:
  FILEOPS_ROOT          If set, every operation path (and MOVE destination) must
                        resolve inside this directory; requests that escape it
                        are rejected with 403. Strongly recommended whenever the
                        server is reachable by anything but the local user.
  FILEOPS_CORS_ORIGINS  Comma-separated allowed origins (default "*").
"""

from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from fileops.core import execute
from fileops.core.models import BatchResult, BatchSpec

app = FastAPI(
    title="fileops",
    description="Atomic batch file operations for AI agent workflows.",
    version="0.3.0",
)

_cors_env = os.environ.get("FILEOPS_CORS_ORIGINS")
_allow_origins = [o.strip() for o in _cors_env.split(",") if o.strip()] if _cors_env else ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allow_origins,
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)


def _within_root(root_real: str, target: str) -> bool:
    target_real = os.path.realpath(os.path.abspath(target))
    try:
        return os.path.commonpath([root_real, target_real]) == root_real
    except ValueError:
        return False  # different drives / unrelated roots


def _confine(spec: BatchSpec, root: str) -> None:
    """Reject any op whose path or destination resolves outside ``root``."""
    root_real = os.path.realpath(os.path.abspath(root))
    for op in spec.operations:
        targets = [op.path]
        if op.destination:
            targets.append(op.destination)
        for t in targets:
            if not _within_root(root_real, t):
                raise PermissionError(f"path {t!r} resolves outside FILEOPS_ROOT")


# ── Routes ────────────────────────────────────────────────────────────────────


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/execute", response_model=BatchResult)
def execute_batch(spec: BatchSpec) -> BatchResult:
    """Execute a batch of file operations atomically."""
    return _run(spec, force_dry_run=False)


@app.post("/dry-run", response_model=BatchResult)
def dry_run_batch(spec: BatchSpec) -> BatchResult:
    """Validate and diff a batch without writing to disk."""
    return _run(spec, force_dry_run=True)


# ── Internal ──────────────────────────────────────────────────────────────────


def _run(spec: BatchSpec, force_dry_run: bool) -> BatchResult:
    if force_dry_run:
        spec = spec.model_copy(update={"dry_run": True})

    # Read at request time so the confinement root can be configured per process
    # and exercised in tests without re-importing the app.
    root = os.environ.get("FILEOPS_ROOT")
    if root:
        try:
            _confine(spec, root)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc))

    try:
        result: BatchResult = execute(spec)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return result
