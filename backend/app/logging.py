"""Structured JSON logging with correlation ID propagation and credential redaction."""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

# Request-scoped correlation ID context variable
current_correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_correlation_id", default=None
)

SENSITIVE_KEY_PATTERN = re.compile(
    r"(?i)(password|secret|token|authorization|bearer|private_key|api_key|credential)"
)
JWT_BEARER_PATTERN = re.compile(r"(?i)Bearer\s+([^\s]+)")


def redact_sensitive_data(obj: Any) -> Any:
    """Recursively scrub sensitive keys and string patterns (tokens, secrets)."""
    if isinstance(obj, dict):
        redacted: dict[str, Any] = {}
        for k, v in obj.items():
            if SENSITIVE_KEY_PATTERN.search(str(k)):
                redacted[k] = "[REDACTED]"
            else:
                redacted[k] = redact_sensitive_data(v)
        return redacted
    elif isinstance(obj, list):
        return [redact_sensitive_data(item) for item in obj]
    elif isinstance(obj, tuple):
        return tuple(redact_sensitive_data(item) for item in obj)
    elif isinstance(obj, str):
        # Scrub Bearer tokens if present in text
        if "bearer" in obj.lower():
            return JWT_BEARER_PATTERN.sub("Bearer [REDACTED]", obj)
        return obj
    return obj


class StructuredJsonFormatter(logging.Formatter):
    """Formats log records as JSON lines with correlation ID and redacted secrets."""

    def __init__(self, service: str = "netci", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        # Obtain message
        message = record.getMessage()
        # Redact raw message text if secrets accidentally leaked
        message = redact_sensitive_data(message)

        corr_id = getattr(record, "correlation_id", None) or current_correlation_id.get()

        data: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": message,
            "service": self.service,
        }
        if corr_id:
            data["correlation_id"] = corr_id

        # Extra attributes
        standard_attrs = {
            "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
            "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
            "created", "msecs", "relativeCreated", "thread", "threadName",
            "processName", "process", "message", "correlation_id"
        }
        extras: dict[str, Any] = {}
        for key, val in record.__dict__.items():
            if key not in standard_attrs and not key.startswith("_"):
                extras[key] = redact_sensitive_data(val)
        if extras:
            data["extra"] = extras

        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)

        return json.dumps(data, default=str)


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """FastAPI/Starlette middleware ensuring every request has a correlation ID."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        corr_id = request.headers.get("X-Correlation-ID") or request.headers.get("x-correlation-id")
        if not corr_id:
            corr_id = uuid4().hex

        token = current_correlation_id.set(corr_id)
        try:
            response = await call_next(request)
            response.headers["X-Correlation-ID"] = corr_id
            return response
        finally:
            current_correlation_id.reset(token)


def configure_logging(level: str = "INFO", json_format: bool = True) -> None:
    """Configure the root logger with either JSON or standard stream formatting."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    if json_format:
        handler.setFormatter(StructuredJsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s")
        )
    root.addHandler(handler)
