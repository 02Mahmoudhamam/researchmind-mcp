"""Memory store interface."""
from abc import ABC, abstractmethod
from typing import Any, Optional
from datetime import timedelta


class BaseMemoryStore(ABC):
    """Interface for session memory operations."""

    @abstractmethod
    async def set(self, key: str, value: Any, ttl: Optional[timedelta] = None) -> bool: ...

    @abstractmethod
    async def get(self, key: str) -> Optional[Any]: ...

    @abstractmethod
    async def delete(self, key: str) -> bool: ...

    @abstractmethod
    async def exists(self, key: str) -> bool: ...

    @abstractmethod
    async def clear_session(self, session_id: str) -> bool: ...
