"""Base class for all brains."""

from __future__ import annotations

from abc import ABC, abstractmethod


class BaseBrain(ABC):
    """Common interface for all brain components."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def is_active(self) -> bool: ...

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...
