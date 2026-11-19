"""Domain errors raised by services. Routers map them to HTTP responses."""


class DomainError(Exception):
    """Base class for expected business errors."""


class NotFoundError(DomainError):
    """The requested entity does not exist (or must not be revealed)."""


class PermissionDeniedError(DomainError):
    """The caller is not allowed to act on this entity."""


class BusinessRuleError(DomainError):
    """The request breaks a business rule."""


class InvalidInputError(DomainError):
    """The request is well-formed but refers to data that does not fit (e.g. unknown venue)."""


class RateLimitedError(DomainError):
    """The same action was repeated too soon; it may be retried after `retry_after` seconds."""

    def __init__(self, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = max(1, retry_after)
