"""Domain errors raised by services. Routers map them to HTTP responses."""


class DomainError(Exception):
    """Base class for expected business errors.

    `code`, when set, is a stable machine-readable reason sent next to `detail`, so a client
    can react to it (show a specific message or action) without parsing the English text.
    """

    code: str | None = None


class NotFoundError(DomainError):
    """The requested entity does not exist (or must not be revealed)."""


class PermissionDeniedError(DomainError):
    """The caller is not allowed to act on this entity."""


class EmailNotVerifiedError(PermissionDeniedError):
    """The action needs a confirmed email address (403, code EMAIL_NOT_VERIFIED)."""

    code = "EMAIL_NOT_VERIFIED"


class BusinessRuleError(DomainError):
    """The request breaks a business rule."""


class PromoNotApplicableError(BusinessRuleError):
    """A valid promo code that applies to none of the chosen tickets (409, BE-29 / R-77)."""

    code = "code_not_applicable"


class InvalidInputError(DomainError):
    """The request is well-formed but refers to data that does not fit (e.g. unknown venue)."""


class RateLimitedError(DomainError):
    """The same action was repeated too soon; it may be retried after `retry_after` seconds."""

    def __init__(self, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = max(1, retry_after)
