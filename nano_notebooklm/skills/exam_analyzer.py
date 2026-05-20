"""Past exam analysis skill — extract patterns from exam papers."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from nano_notebooklm.ai import prompt_templates as prompts
from nano_notebooklm import config
from nano_notebooklm.skills.base import Skill
from nano_notebooklm.types import SkillResult

logger = logging.getLogger(__name__)


class ExamAnalyzerSkill(Skill):
    name = "exam_analyzer"
    description = "Analyze past exams to identify patterns and high-frequency topics"

    async def execute(self, params: dict) -> SkillResult:
        """
        Params:
            course_id (str): Course identifier
            exam_doc_ids (list[str] | None): Specific exam documents to analyze
        """
        course_id = params.get("course_id", "")
        if not course_id:
            return SkillResult(success=False, error="No course_id provided")

        # 1. Find exam-related content
        # Search for exam/test/quiz/midterm/final keywords
        exam_queries = [
            "exam midterm final test quiz",
            "problem solution exercise question",
            "homework assignment practice",
        ]

        all_results = []
        for query in exam_queries:
            results = self.kb.search(query, top_k=10, course_id=course_id)
            all_results.extend(results)

        # Deduplicate by chunk_id
        seen = set()
        unique_results = []
        for r in all_results:
            if r.chunk_id not in seen:
                seen.add(r.chunk_id)
                unique_results.append(r)

        if not unique_results:
            # Fallback: use all course content for pattern analysis
            chunks = self.kb.get_chunks(course_id)
            if not chunks:
                return SkillResult(success=False, error="No content found for analysis")
            # Use general content
            from nano_notebooklm.types import SearchResult
            unique_results = [
                SearchResult(
                    chunk_id=c.chunk_id, text=c.text, source_file=c.source_file,
                    location=c.location, score=0.0, course_id=c.course_id,
                )
                for c in chunks[:20]
            ]

        exam_text = "\n\n---\n\n".join(
            f"[Source: {r.source_file}, {r.location}]\n{r.text}"
            for r in unique_results
        )

        # 2. Analyze via LLM
        prompt = prompts.EXAM_ANALYSIS_PROMPT.format(exam_text=exam_text)

        try:
            analysis = await self.router.complete_structured(
                prompt,
                task_type="exam_analysis",
                system="You are an expert at analyzing academic exam patterns. Output valid JSON only.",
                temperature=0.3,
            )
        except Exception:
            logger.exception("exam_analysis LLM call failed")
            return SkillResult(success=False, error="exam_analysis_failed")

        # 3. Save analysis
        output_dir = config.ARTIFACTS_DIR / "courses" / course_id
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / "exam_analysis.json"
        output_path.write_text(json.dumps(analysis, ensure_ascii=False, indent=2))

        return SkillResult(
            success=True,
            output_path=str(output_path),
            data=analysis,
        )
