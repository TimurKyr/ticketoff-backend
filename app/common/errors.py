"""Domain errors raised by services. Routers map them to HTTP responses."""


class DomainError(Exception):
    """Base class for expected business errors."""


class NotFoundError(DomainError):
    """The requested entity does not exist (or must not be revealed)."""


class PermissionDeniedError(DomainError):
    """The caller is not allowed to act on this entity."""


class BusinessRuleError(DomainError):
    """The request breaks a business rule."""
