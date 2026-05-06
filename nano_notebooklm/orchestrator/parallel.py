"""Parallel skill execution using asyncio."""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from nano_notebooklm import config
from nano_notebooklm.skills.base import Skill
from nano_notebooklm.types import SkillResult

logger = logging.getLogger(__name__)


class ParallelRunner:
    """Run multiple skills concurrently."""

    def __init__(self, max_workers: int = config.MAX_PARALLEL_WORKERS):
        self.max_workers = max_workers

    async def run_parallel(
        self, tasks: list[tuple[Skill, dict]]
    ) -> list[SkillResult]:
        """Run independent skills concurrently.

        Args:
            tasks: List of (skill, params) tuples
        """
        coros = [skill.execute(params) for skill, params in tasks]
        results = await asyncio.gather(*coros, return_exceptions=True)

        processed = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                skill_name = tasks[i][0].name
                logger.error(f"Skill {skill_name} failed: {result}")
                processed.append(SkillResult(success=False, error=str(result)))
            else:
                processed.append(result)

        return processed

    async def run_sequential(
        self, tasks: list[tuple[Skill, dict]]
    ) -> list[SkillResult]:
        """Run dependent skills in order."""
        results = []
        for skill, params in tasks:
            try:
                result = await skill.execute(params)
                results.append(result)
                if not result.success:
                    logger.warning(f"Skill {skill.name} failed, stopping chain: {result.error}")
                    break
            except Exception as e:
                logger.error(f"Skill {skill.name} raised: {e}")
                results.append(SkillResult(success=False, error=str(e)))
                break
        return results
