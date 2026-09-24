"""Which change freeze, if any, stops a deployment (ADR-047). Pure: freezes in, answer out."""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from ..store.records import ChangeFreezeRecord


def applicable_freeze(
    freezes: Iterable[ChangeFreezeRecord],
    *,
    environment: str,
    system_id: str | None,
    module_id: str | None,
    at: datetime,
) -> ChangeFreezeRecord | None:
    """The freeze covering `at` in `environment` for this module, else None.

    A freeze with no system and no module covers everything; with a system, every module
    of it; with a module, that module. The window is half-open, [starts_at, ends_at): at
    ends_at the freeze is over. When several apply, the one ending last is the answer,
    because that is when the deployment could next go.
    """

    matching = [
        f for f in freezes
        if f.cancelled_at is None
        and f.starts_at <= at < f.ends_at
        and environment in f.environments
        and (f.system_id is None or f.system_id == system_id)
        and (f.module_id is None or f.module_id == module_id)
    ]
    return max(matching, key=lambda f: f.ends_at) if matching else None
