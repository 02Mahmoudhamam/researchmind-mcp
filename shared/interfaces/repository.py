"""Base repository interface for data access layer."""

from abc import ABC, abstractmethod
from typing import Generic, TypeVar, Optional, List

T = TypeVar("T")
ID = TypeVar("ID")


class BaseRepository(ABC, Generic[T, ID]):
    """Generic repository interface."""

    @abstractmethod
    async def get_by_id(self, id: ID) -> Optional[T]: ...

    @abstractmethod
    async def get_all(self) -> List[T]: ...

    @abstractmethod
    async def create(self, entity: T) -> T: ...

    @abstractmethod
    async def update(self, id: ID, entity: T) -> Optional[T]: ...

    @abstractmethod
    async def delete(self, id: ID) -> bool: ...
