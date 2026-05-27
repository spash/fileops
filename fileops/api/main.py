"""
fileops API server

POST /execute     — run a BatchSpec, return BatchResult
POST /dry-run     — same but forces dry_run=True; writes nothing

Start with: uvicorn fileops.api.main:app --reload
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from fileops.core import execute
from fileops.core.models import BatchResult, BatchSpec

app = FastAPI(
    title="fileops",
    description="Atomic batch file operations for AI agent workflows.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in production
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)


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

    try:
        result: BatchResult = execute(spec)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return result
