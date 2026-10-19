"""Shared domain errors (mapped to HTTP 4xx by the API layer)."""


class DomainError(Exception):
    """Domain rule violation."""


class NotFound(DomainError):
    pass


class Invalid(DomainError):
    pass


# Backwards-compatible alias (catalog endpoints historically caught CatalogError).
CatalogError = DomainError
