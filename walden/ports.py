from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any
from .core.models import EntityHead

class BlobStore(ABC):
    @abstractmethod
    async def put(self, data: bytes) -> str: ...
    @abstractmethod
    async def get(self, blob_id: str) -> bytes: ...

class EntityRegistry(ABC):
    @abstractmethod
    async def create(self, kind: str, key_hash: str, head_blob_id: str) -> EntityHead: ...
    @abstractmethod
    async def get(self, object_id: str) -> EntityHead: ...
    @abstractmethod
    async def update(self, object_id: str, expected_revision: int, new_head_blob_id: str) -> EntityHead: ...

class BindingStore(ABC):
    @abstractmethod
    async def get(self, kind: str, key_hash: str) -> str | None: ...
    @abstractmethod
    async def put(self, kind: str, key_hash: str, object_id: str) -> None: ...

class SemanticIndex(ABC):
    @abstractmethod
    async def remember(self, text: str, namespace: str) -> str | None: ...
    @abstractmethod
    async def recall(self, query: str, namespaces: list[str], limit: int) -> list[dict[str, Any]]: ...
    async def close(self) -> None: pass
