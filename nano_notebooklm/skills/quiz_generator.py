"""Practice test / quiz generation skill."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from nano_notebooklm.ai import prompt_templates as prompts
from nano_notebooklm import config
from nano_notebooklm.skills.base import Skill
from nano_notebooklm.types import SkillResult

logger = logging.getLogger(__name__)


class QuizGeneratorSkill(Skill):
    name = "quiz_generator"
    description = "Generate practice tests based on course materials and exam patterns"

    async def execute(self, params: dict) -> SkillResult:
        """
        Params:
            course_id (str): Course identifier
            topic (str | None): Focus on specific topic
            num_questions (int): Number of questions (default 10)
            difficulty (str): "easy" | "medium" | "hard" (default "medium")
            question_types (list[str]): Types to include
            weak_concepts (list[str] | None): Concepts to emphasize
        """
        course_id = params.get("course_id", "")
        topic = params.get("topic")
        num_questions = params.get("num_questions", 10)
        difficulty = params.get("difficulty", "medium")
        question_types = params.get("question_types", ["multiple_choice", "short_answer", "calculation"])
        weak_concepts = params.get("weak_concepts")

        if not course_id:
            return SkillResult(success=False, error="No course_id provided")

        # 1. Retrieve relevant content
        query = topic if topic else f"key concepts for {course_id}"
        results = self.kb.search(query, top_k=15, course_id=course_id)

        if not results:
            return SkillResult(success=False, error="No content found for quiz generation")

        source_text = "\n\n---\n\n".join(
            f"[Source: {r.source_file}, {r.location}]\n{r.text}"
            for r in results
        )

        # 2. Build weak concepts instruction
        weak_instruction = ""
        if weak_concepts:
            weak_instruction = (
                f"\nIMPORTANT: The student is weak in these areas, emphasize them: "
                f"{', '.join(weak_concepts)}"
            )

        # 3. Generate quiz via LLM
        prompt = prompts.QUIZ_GENERATION_PROMPT.format(
            num_questions=num_questions,
            course_name=course_id,
            topic=topic or "General",
            difficulty=difficulty,
            question_types=", ".join(question_types),
            weak_concepts_instruction=weak_instruction,
            source_text=source_text,
        )

        try:
            quiz_data = await self.router.complete_structured(
                prompt,
                task_type="quiz_generation",
                system=prompts.QUIZ_GENERATION_SYSTEM,
                temperature=0.7,
            )
        except Exception as e:
            logger.error(f"Quiz generation failed: {e}")
            return SkillResult(success=False, error=str(e))

        # Handle both list and dict responses
        if isinstance(quiz_data, list):
            questions = quiz_data
        else:
            questions = quiz_data.get("questions", quiz_data.get("quiz", []))

        # 4. Save quiz
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = config.ARTIFACTS_DIR / "courses" / course_id / "quizzes"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"quiz_{timestamp}.json"
        output_path.write_text(json.dumps(questions, ensure_ascii=False, indent=2))

        return SkillResult(
            success=True,
            output_path=str(output_path),
            data={
                "quiz": questions,
                "num_questions": len(questions),
                "topic": topic or "General",
                "difficulty": difficulty,
            },
        )
