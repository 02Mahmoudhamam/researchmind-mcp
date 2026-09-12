"""Identifier handling at the repository boundary.

The domain models use `id: str`; the database uses native `UUID`. Conversion
happens here, at the edge, so neither side has to know about the other's
representation.
"""

import uuid


def parse_id(value: str | uuid.UUID | None) -> uuid.UUID | None:
    """Return a UUID, or None if `value` is not one.

    Returning None rather than raising is deliberate. Identifiers reach a
    repository from request paths and MCP tool arguments, so a malformed one is
    attacker-supplied input, not a programming error. If this raised, a request
    for `/documents/../../etc/passwd` would be a 500 — an unhandled exception is
    both a worse experience and a louder signal to whoever sent it than a plain
    404.

    Callers treat None the same way they treat "no such row", which is also
    what makes "does not exist" and "not yours" indistinguishable.
    """
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None
