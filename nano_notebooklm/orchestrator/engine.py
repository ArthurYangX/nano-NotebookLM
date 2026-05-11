"""Main orchestrator: routes user intent to skills."""

from __future__ import annotations

import logging

from nano_notebooklm.ai.router import ModelRouter
from nano_notebooklm.kb.store import KBStore
from nano_notebooklm.orchestrator.checkpoint import CheckpointManager
from nano_notebooklm.orchestrator.parallel import ParallelRunner
from nano_notebooklm.orchestrator.session import Session
from nano_notebooklm.skills.base import Skill
from nano_notebooklm.skills.exam_analyzer import ExamAnalyzerSkill
from nano_notebooklm.skills.exam_prep import ExamPrepSkill
from nano_notebooklm.skills.mastery_tracker import MasteryTrackerSkill
from nano_notebooklm.skills.note_generator import NoteGeneratorSkill
from nano_notebooklm.skills.qa_skill import QASkill
from nano_notebooklm.skills.quiz_generator import QuizGeneratorSkill
from nano_notebooklm.skills.report_generator import ReportGeneratorSkill
from nano_notebooklm.types import SkillResult

logger = logging.getLogger(__name__)


class Orchestrator:
    """Central orchestrator that routes user requests to appropriate skills."""

    def __init__(self, kb: KBStore, router: ModelRouter):
        self.kb = kb
        self.router = router
        self.checkpoint = CheckpointManager()
        self.parallel = ParallelRunner()
        self.session = Session()

        # Register all skills
        self.skills: dict[str, Skill] = {
            "qa": QASkill(kb, router),
            "note_generator": NoteGeneratorSkill(kb, router),
            "quiz_generator": QuizGeneratorSkill(kb, router),
            "exam_analyzer": ExamAnalyzerSkill(kb, router),
            "exam_prep": ExamPrepSkill(kb, router),
            "mastery_tracker": MasteryTrackerSkill(kb, router),
            "report_generator": ReportGeneratorSkill(kb, router),
        }

    async def handle(self, user_message: str, course_filter: str | None = None) -> SkillResult:
        """Handle a user message by routing to the appropriate skill."""
        self.session.add_message("user", user_message)

        # Direct QA routing for chat messages
        result = await self.skills["qa"].execute({
            "question": user_message,
            "course_filter": course_filter,
        })

        if result.success:
            answer = result.data.get("answer", "")
            self.session.add_message("assistant", answer)

        return result

    async def run_skill(self, skill_name: str, params: dict) -> SkillResult:
        """Run a specific skill by name."""
        if skill_name not in self.skills:
            return SkillResult(success=False, error=f"Unknown skill: {skill_name}")
        return await self.skills[skill_name].execute(params)

    async def run_parallel_skills(self, tasks: list[tuple[str, dict]]) -> list[SkillResult]:
        """Run multiple skills in parallel."""
        skill_tasks = []
        for skill_name, params in tasks:
            if skill_name in self.skills:
                skill_tasks.append((self.skills[skill_name], params))
        return await self.parallel.run_parallel(skill_tasks)

    def register_skill(self, skill: Skill):
        """Register a new skill."""
        self.skills[skill.name] = skill

    def list_courses(self) -> list[str]:
        """List available courses."""
        courses_dir = self.kb.artifacts_dir / "courses"
        if not courses_dir.exists():
            return []
        return sorted(d.name for d in courses_dir.iterdir() if d.is_dir())
