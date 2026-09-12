"""Resolve runtime storage outside the installed skill package."""

from __future__ import annotations

from pathlib import Path


WORKSPACE_DIRECTORY_NAME = "buffett-read-financial-statements"


def default_workspace_directory(base: Path | None = None) -> Path:
    """Return the dedicated storage directory inside the active workspace."""
    current = (base or Path.cwd()).expanduser().resolve()
    if current.name == WORKSPACE_DIRECTORY_NAME:
        return current
    return current / WORKSPACE_DIRECTORY_NAME


def default_data_directory(base: Path | None = None) -> Path:
    return default_workspace_directory(base) / "data"


def default_database_path(base: Path | None = None) -> Path:
    return default_data_directory(base) / "eastmoney_financials.sqlite3"


def default_analysis_directory(base: Path | None = None) -> Path:
    return default_data_directory(base) / "analysis"
