"""Shared domain errors (mapped to HTTP 4xx by the API layer)."""


class DomainError(Exception):
    """Domain rule violation."""


class NotFound(DomainError):
    pass


class Invalid(DomainError):
    pass


class Conflict(DomainError):
    """The entity changed after the action was prepared (HTTP 409)."""


# Backwards-compatible alias (catalog endpoints historically caught CatalogError).
CatalogError = DomainError
