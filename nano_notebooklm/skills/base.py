"""Abstract Skill base class."""

from __future__ import annotations

from abc import ABC, abstractmethod

from nano_notebooklm.ai.router import ModelRouter
from nano_notebooklm.kb.store import KBStore
from nano_notebooklm.types import SkillResult


class Skill(ABC):
    """Base class for all nano-NOTEBOOKLM skills."""

    name: str
    description: str

    def __init__(self, kb: KBStore, router: ModelRouter):
        self.kb = kb
        self.router = router

    @abstractmethod
    async def execute(self, params: dict) -> SkillResult:
        """Execute the skill with the given parameters."""
        ...
