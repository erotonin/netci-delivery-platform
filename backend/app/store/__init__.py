"""The one seam between netCI's domain logic and its durable state.

The whole interface is `PlatformDatabase.transaction()`. Everything a request needs to
read and write lives on the `PlatformSession` it yields, and everything that makes that
hard -- connections, SQL, row mapping, version compare-and-set, rollback -- lives inside
an implementation the caller never names.

Two implementations exist and they are not interchangeable at will:

* `PostgresDatabase` is the only one a non-local runtime may use.
* `InMemoryDatabase` exists so unit tests can exercise domain rules without a database,
  and so `NETCI_ENVIRONMENT=local` works on a laptop with nothing installed. It stages
  writes and discards them if the transaction raises, so a test that injects a fault
  observes the same rollback a real transaction would give it.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Protocol

from ..persistence import database_url
from .memory import InMemoryDatabase
from .postgres import PostgresDatabase
from .records import ModuleRow, RequestModuleRow, RequestRow, SystemRow, VersionRow
from .session import PlatformSession

__all__ = [
    "InMemoryDatabase",
    "join",
    "ModuleRow",
    "PlatformDatabase",
    "PlatformSession",
    "PostgresDatabase",
    "RequestModuleRow",
    "RequestRow",
    "SystemRow",
    "VersionRow",
    "build_database",
]


class PlatformDatabase(Protocol):
    """Hand out transactions; hold no state of its own."""

    def transaction(self):
        """A context manager yielding a `PlatformSession`.

        The transaction commits when the block exits normally and rolls back when it
        raises. Nesting joins the outer transaction rather than opening a second one,
        so a command called inside a composite operation cannot commit half of it.
        """

    def health(self) -> str:
        """"ok", or a message explaining why this store cannot be reached."""

    def describe(self) -> str:
        """Short mode name for /healthz -- never a connection string."""


def build_database() -> PlatformDatabase:
    """Choose the store from configuration, and refuse the unsafe combination.

    An in-memory store outside local mode would mean accepting an approval, reporting
    success and losing it on the next deploy. That is worse than refusing to start, so
    a missing `DATABASE_URL` is a startup failure everywhere except explicit local mode.
    """

    url = database_url()
    environment = os.getenv("NETCI_ENVIRONMENT", "local").strip().lower()
    if url:
        return PostgresDatabase(url)
    if environment != "local":
        raise RuntimeError(
            "DATABASE_URL (or DATABASE_URL_FILE) is required outside local mode: "
            "netCI will not serve durable state from process memory"
        )
    return InMemoryDatabase()


@contextmanager
def join(database: PlatformDatabase, session: PlatformSession | None):
    """Run inside `session` if the caller already has one, otherwise open a transaction.

    This is how a command stays composable without ever committing half of a larger
    operation: `create_application` opens its own transaction when called directly, and
    joins the onboarding transaction when called as part of it. Passing the session
    explicitly -- rather than tracking "the current transaction" on the database -- keeps
    it correct when FastAPI runs two requests on two threads.
    """

    if session is not None:
        yield session
        return
    with database.transaction() as opened:
        yield opened
