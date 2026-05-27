"""
Core data models for fileops.

These are the stable contracts between all layers — CLI, API, and executor.
Changing these is a breaking change; changing implementations is not.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_serializer, model_validator


class OperationType(str, Enum):
    CREATE = "create"
    WRITE = "write"
    DELETE = "delete"
    MOVE = "move"


class FileOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: OperationType
    path: str
    content: Optional[str] = None       # required for CREATE, WRITE
    destination: Optional[str] = None   # required for MOVE

    @model_validator(mode="after")
    def _validate_fields(self) -> FileOperation:
        if self.type in (OperationType.CREATE, OperationType.WRITE):
            if self.content is None:
                raise ValueError(f"'{self.type.value}' operation requires 'content'")
        if self.type == OperationType.MOVE:
            if not self.destination:
                raise ValueError("'move' operation requires 'destination'")
        return self

    def summary(self) -> str:
        if self.type == OperationType.MOVE:
            return f"{self.type.name}: {self.path} → {self.destination}"
        return f"{self.type.name}: {self.path}"


class BatchSpec(BaseModel):
    operations: list[FileOperation] = Field(min_length=1)
    description: Optional[str] = None
    dry_run: bool = False

    @property
    def operation_count(self) -> int:
        return len(self.operations)


class OperationResult(BaseModel):
    operation: FileOperation
    success: bool
    diff: Optional[str] = None   # unified diff; None for MOVE only
    error: Optional[str] = None

    @model_serializer
    def _serialize(self) -> dict:
        return {
            "type": self.operation.type.value,
            "path": self.operation.path,
            "destination": self.operation.destination,
            "success": self.success,
            "diff": self.diff,
            "error": self.error,
        }


class BatchResult(BaseModel):
    success: bool
    results: list[OperationResult]
    rolled_back: bool = False
    error: Optional[str] = None

    @computed_field
    @property
    def operation_count(self) -> int:
        return len(self.results)

    @computed_field
    @property
    def success_count(self) -> int:
        return sum(1 for r in self.results if r.success)


