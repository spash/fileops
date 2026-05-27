"""
Core exceptions for fileops.
"""

class FileOpsError(Exception):
    """Base exception for all fileops errors."""

class ExecutorStateError(FileOpsError):
    """Raised when the executor's internal state is corrupted."""

class RollbackFailedWarning(UserWarning):
    """Emitted when a rollback step fails."""
