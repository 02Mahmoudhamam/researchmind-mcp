"""Local-volume storage — ADR-0008's single-node backend.

Writes `{root}/{user_id}/{sha256}.pdf`. Three properties matter, and each is
tested:

* **Keys are checked before any path exists.** Anything not shaped like
  `document_storage_key`'s output is refused, and the resolved path must still
  lie inside the root. The first check makes traversal unexpressible; the
  second is there in case the first is ever loosened.
* **A write is atomic.** Bytes go to a temporary file in the destination
  directory and are hard-linked into place. A crash mid-write leaves a stray
  temporary file, never a truncated object at a real key — which matters more
  than usual here, because content addressing would treat anything at a key as
  the correct bytes forever.
* **`put` knows whether it created the object.** `os.link` fails if the target
  exists, atomically, so two concurrent uploads of the same content cannot both
  believe they wrote it. Only the one that did may later delete it (ADR-0010).

Filesystem calls are blocking, so they run in a worker thread rather than on
the event loop.
"""

import os
import tempfile
from pathlib import Path

import anyio.to_thread

from shared.interfaces.storage import (
    STORAGE_KEY_PATTERN,
    StorageError,
    StorageObjectMissing,
)


class LocalStorage:
    """`Storage` over a directory on the local filesystem."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        # Resolved once, without requiring the directory to exist yet — it is
        # created on first write, so constructing a LocalStorage never touches
        # the disk and a provider can build one per request.
        self._root = Path(root).resolve()

    @property
    def root(self) -> Path:
        return self._root

    def _path_for(self, key: str) -> Path:
        """The filesystem path for a key, or StorageError if the key is not one.

        Deliberately strict. `fullmatch` on the ADR-0008 pattern rejects `..`,
        absolute paths, backslashes, NUL bytes, uppercase variants and anything
        with more than one separator before a path is ever constructed.
        """
        if not isinstance(key, str) or not STORAGE_KEY_PATTERN.fullmatch(key):
            raise StorageError("refused a key that is not a document storage key")
        path = (self._root / key).resolve()
        if not path.is_relative_to(self._root):
            # Unreachable while the pattern holds. It is the second lock on the
            # same door, for the day someone widens the first.
            raise StorageError("refused a key that resolves outside the storage root")
        return path

    async def put(self, key: str, data: bytes) -> bool:
        path = self._path_for(key)
        return await anyio.to_thread.run_sync(self._put_sync, path, data)

    @staticmethod
    def _put_sync(path: Path, data: bytes) -> bool:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                return False

            descriptor, temporary = tempfile.mkstemp(
                dir=path.parent, prefix=".upload-", suffix=".tmp"
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    # Atomic create-if-absent. `os.replace` would also be atomic
                    # but overwrites silently, so it cannot say who created the
                    # object; `link` can.
                    os.link(temporary, path)
                except FileExistsError:
                    return False
                LocalStorage._fsync_directory(path.parent)
                return True
            finally:
                Path(temporary).unlink(missing_ok=True)
        except StorageError:
            raise
        except OSError as exc:
            raise StorageError(f"storage put failed: {type(exc).__name__}") from exc

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        """Make the new directory entry durable, not just the file's contents."""
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    async def get(self, key: str) -> bytes:
        path = self._path_for(key)
        return await anyio.to_thread.run_sync(self._get_sync, path)

    @staticmethod
    def _get_sync(path: Path) -> bytes:
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise StorageObjectMissing("no object is stored at that key") from exc
        except OSError as exc:
            raise StorageError(f"storage get failed: {type(exc).__name__}") from exc

    async def delete(self, key: str) -> None:
        path = self._path_for(key)
        await anyio.to_thread.run_sync(self._delete_sync, path)

    @staticmethod
    def _delete_sync(path: Path) -> None:
        # The owner's directory is left in place even when it empties: removing
        # it would race a concurrent `put` that has just created it.
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise StorageError(f"storage delete failed: {type(exc).__name__}") from exc
