"""Human-in-the-loop checkpoint gates."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from nano_notebooklm import config

logger = logging.getLogger(__name__)


@dataclass
class CheckpointDecision:
    proceed: bool
    modified_params: dict | None = None
    feedback: str = ""


class CheckpointManager:
    """Manages human-in-the-loop gates at critical junctions."""

    def __init__(self, mode: str | None = None):
        self.mode = mode or config.CHECKPOINT_MODE
        self._approval_callback = None

    def set_approval_callback(self, callback):
        """Set a callback for interactive approval (used by UI)."""
        self._approval_callback = callback

    async def gate(self, action: str, preview: dict) -> CheckpointDecision:
        """Check if action should proceed or needs human approval.

        Args:
            action: Description of the action (e.g. "Generate notes for CS231N")
            preview: Preview of what will happen
        """
        if self.mode == "auto":
            logger.info(f"Auto-proceeding: {action}")
            return CheckpointDecision(proceed=True)

        if self._approval_callback:
            return await self._approval_callback(action, preview)

        # Default: proceed (no UI connected)
        logger.info(f"No approval callback set, proceeding: {action}")
        return CheckpointDecision(proceed=True)
