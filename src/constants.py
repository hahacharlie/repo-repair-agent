"""Shared values used across the repair agent."""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "config.json"
DEFAULT_ENV = PROJECT_ROOT / ".env"
BROKEN_REPOS = PROJECT_ROOT / "input" / "broken_repos"

API_KEY_ENV = "ASU_AIR_API_KEY"
MINIMUM_PYTHON = (3, 10)

DEFAULT_API_TIMEOUT = 300
MINIMUM_API_TIMEOUT = 1
MAXIMUM_API_TIMEOUT = 3600

DEFAULT_PYTHON_IMAGE = "python:3.12-slim"
