"""Base repository interface for data access layer.

.. warning::

   **Do not inherit this for a user-owned resource.** It has no implementations
   and, as of Sprint M1/S1.3, none are planned.

   Every method below reaches a row by id alone. For ``users`` that is fine — a
   user is the ownership root. For ``documents`` and ``document_chunks`` it is
   not: ``docs/security/principles.md`` §3 requires the ownership filter to be
   "a **required parameter of the repository signature**, not an optional
   ``filters`` dict entry", and states that "the safe path must be the only
   path". ``get_all()`` cannot satisfy that at all, and ``get_by_id``,
   ``update`` and ``delete`` would each need an owner the signature does not
   have.

   This partially supersedes ADR-0003 §3, which asked for ``UserRepository``,
   ``DocumentRepository`` and ``ChunkRepository`` to be implementations of this
   ABC. ``principles.md`` is labelled non-negotiable and an ADR is by
   construction revisitable, so the invariant wins. The three repositories in
   ``backend/db/repositories/`` are explicit classes instead.

   Kept rather than deleted because ADR-0003 still references it by name and
   because a future non-owned entity may legitimately use it. If nothing claims
   it by the end of M1, delete it — an unused abstraction that looks blessed is
   a trap.
"""

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
