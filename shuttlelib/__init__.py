"""Structured session records for Shuttle launchers and provider hooks."""

from .sessions import (
    BindingConflict,
    ClosedLaunch,
    CorruptRecord,
    InvalidRecord,
    InvalidTransition,
    LaunchNotFound,
    Registry,
    RegistryError,
    ResumeIdentityConflict,
    ScanIssue,
    ScanResult,
)

__all__ = [
    "BindingConflict",
    "ClosedLaunch",
    "CorruptRecord",
    "InvalidRecord",
    "InvalidTransition",
    "LaunchNotFound",
    "Registry",
    "RegistryError",
    "ResumeIdentityConflict",
    "ScanIssue",
    "ScanResult",
]
