"""Domain errors that map cleanly onto HTTP problem responses (RFC 9457)."""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """Base class for expected, user-facing errors."""

    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.details = details


class ValidationFailed(DomainError):
    status_code = 422
    code = "validation_failed"


class AuthenticationRequired(DomainError):
    status_code = 401
    code = "authentication_required"


class PermissionDenied(DomainError):
    status_code = 403
    code = "permission_denied"


class NotFound(DomainError):
    status_code = 404
    code = "not_found"


class Conflict(DomainError):
    status_code = 409
    code = "conflict"


class QuotaExceeded(DomainError):
    """Raised when a tenant has exhausted its plan's usage allowance.

    402 Payment Required is deliberate: it is the product-led-growth moment where the
    client should show an upgrade prompt rather than a generic error.
    """

    status_code = 402
    code = "quota_exceeded"


class RateLimited(DomainError):
    status_code = 429
    code = "rate_limited"


class UpstreamUnavailable(DomainError):
    status_code = 503
    code = "upstream_unavailable"


class TenantContextMissing(RuntimeError):
    """Programming error: tenant-scoped code ran without a tenant in context."""
