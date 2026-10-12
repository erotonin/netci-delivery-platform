"""The one shape a failure takes on its way to a client.

`DeliveryError` and `PortalError` both mean "this request has an answer, and it is not
200". Giving them a common base lets code that composes commands from both layers -- module
onboarding writes through the delivery domain and the Portal in one transaction -- tell a
decided answer apart from a storage failure it should turn into a 503. Without it, a
`404 SYSTEM_NOT_FOUND` raised inside a transaction comes back as "the database is down".
"""

from __future__ import annotations


class ApiError(RuntimeError):
    """A failure that already knows what the client should be told."""

    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
