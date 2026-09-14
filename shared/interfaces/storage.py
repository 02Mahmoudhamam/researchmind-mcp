"""Object storage for uploaded documents — the seam ADR-0008 defines.

Two things live here, and both are part of the contract rather than of any one
backend:

* `Storage`, the protocol a backend implements: `put`, `get`, `delete`.
* `document_storage_key`, the **only** way a key is made. ADR-0008 fixes the
  layout as `{user_id}/{sha256}.pdf`, and a future S3 backend has to use the
  same keys as the local one, so key construction cannot belong to either.

The client's filename appears in neither. That is the point of ADR-0008 §3:
path traversal is not defended against here, it is made unexpressible — a key is
built from a UUID and 64 hex characters, and there is no argument through which
a `..` could arrive.
"""

import re
import uuid
from typing import Protocol

# `{uuid}/{sha256}.pdf`, and nothing else. Lowercase, canonical, no separators
# other than the one. Backends check keys against this before touching storage,
# so a key that did not come from `document_storage_key` cannot reach a path.
STORAGE_KEY_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/[0-9a-f]{64}\.pdf$"
)

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class StorageError(Exception):
    """Storage could not complete an operation.

    The message names the operation and the kind of failure, never a path: a
    path contains the owner's id and the document's hash, and this exception's
    text can travel further than a log line.
    """


class StorageObjectMissing(StorageError):
    """The key names nothing in storage."""


def document_storage_key(owner_id: str, content_hash: str) -> str:
    """The storage key for a document: `{owner_id}/{content_hash}.pdf`.

    Both inputs are trusted values — the owner from `Principal.user_id`, the
    hash from hashing the bytes — so a malformed one is a programming error and
    raises rather than being quietly normalised into something else.

    The owner id is canonicalised through `uuid.UUID`, so `ABCD…` and `abcd…`
    cannot become two directories for one user.

    :raises ValueError: if either part is not what it must be.
    """
    try:
        owner = str(uuid.UUID(owner_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("owner_id must be a UUID") from exc
    if not isinstance(content_hash, str) or not _SHA256_HEX.fullmatch(content_hash):
        raise ValueError("content_hash must be 64 lowercase hexadecimal characters")
    return f"{owner}/{content_hash}.pdf"


class Storage(Protocol):
    """Where uploaded bytes live. Keys come from `document_storage_key`."""

    async def put(self, key: str, data: bytes) -> bool:
        """Store `data` at `key`. Returns True only if this call created it.

        Content-addressed, so an object already at the key holds the same bytes
        and is left as it is. The return value is what lets a caller undo its
        *own* write after a later failure without deleting an object that
        another document already depends on (ADR-0010).

        :raises StorageError: if the bytes could not be stored.
        """
        ...

    async def get(self, key: str) -> bytes:
        """Return the bytes at `key`.

        :raises StorageObjectMissing: if nothing is stored there.
        :raises StorageError: on any other failure.
        """
        ...

    async def delete(self, key: str) -> None:
        """Remove the object at `key`. Removing something absent is not an error.

        :raises StorageError: if an existing object could not be removed.
        """
        ...
