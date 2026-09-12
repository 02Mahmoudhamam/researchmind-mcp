"""Unique ID generation utilities."""

import uuid


def generate_id() -> str:
    return str(uuid.uuid4())


def generate_short_id() -> str:
    return str(uuid.uuid4())[:8]
