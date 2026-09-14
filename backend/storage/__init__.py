"""Storage backends for uploaded documents. The contract is `shared/interfaces/storage.py`."""

from backend.storage.local import LocalStorage

__all__ = ["LocalStorage"]
