"""One definition of when netCI may use development-only integration modes."""

from __future__ import annotations

import os


def is_local_runtime() -> bool:
    return os.getenv("NETCI_ENVIRONMENT", "local").strip().lower() == "local"


def require_live_mode(variable: str, mode: str, *, disabled: set[str] | None = None) -> None:
    """Refuse a no-op integration outside the explicitly local runtime."""

    disabled_modes = disabled or {"", "none", "callback"}
    if not is_local_runtime() and mode in disabled_modes:
        raise RuntimeError(f"{variable} must select a live integration outside local mode")
