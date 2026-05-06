"""Structured note generation skill (LaTeX/Markdown)."""

from __future__ import annotations

import logging
from pathlib import Path

from nano_notebooklm.ai import prompt_templates as prompts
from nano_notebooklm import config
from nano_notebooklm.skills.base import Skill
from nano_notebooklm.types import NoteFormat, SkillResult

logger = logging.getLogger(__name__)


class NoteGeneratorSkill(Skill):
    name = "note_generator"
    description = "Generate structured study notes from course materials"

    async def execute(self, params: dict) -> SkillResult:
        """
        Params:
            course_id (str): Course identifier
            topic (str | None): Specific topic to focus on (None = full course)
            format (str): "markdown" or "latex"
            scope (str): "lecture" | "chapter" | "full"
        """
        course_id = params.get("course_id", "")
        topic = params.get("topic")
        note_format = params.get("format", "markdown")
        scope = params.get("scope", "full")

        if not course_id:
            return SkillResult(success=False, error="No course_id provided")

        # 1. Retrieve relevant chunks
        if topic:
            results = self.kb.search(topic, top_k=15, course_id=course_id)
        else:
            # Get all chunks for the course (limited for API cost)
            chunks = self.kb.get_chunks(course_id)
            if not chunks:
                return SkillResult(success=False, error=f"No chunks found for course {course_id}")
            # Sample representative chunks
            results = self.kb.search(
                f"key concepts definitions theorems examples for {course_id}",
                top_k=20,
                course_id=course_id,
            )

        if not results:
            return SkillResult(success=False, error="No relevant content found")

        # 2. Build source text
        source_text = "\n\n---\n\n".join(
            f"[Source: {r.source_file}, {r.location}]\n{r.text}"
            for r in results
        )

        # 3. Format instructions
        if note_format == "latex":
            format_instructions = prompts.NOTE_FORMAT_LATEX
        else:
            format_instructions = prompts.NOTE_FORMAT_MARKDOWN

        # 4. Generate notes via LLM
        prompt = prompts.NOTE_GENERATION_PROMPT.format(
            course_name=course_id,
            topic=topic or "Full Course Overview",
            format=note_format,
            source_text=source_text,
            format_instructions=format_instructions,
        )

        resp = await self.router.complete(
            prompt,
            task_type="note_generation",
            system=prompts.NOTE_GENERATION_SYSTEM,
            temperature=0.3,
            max_tokens=8192,
        )

        # 5. Save to file
        ext = ".tex" if note_format == "latex" else ".md"
        topic_slug = _slugify(topic) if topic else "full_course"
        output_dir = config.ARTIFACTS_DIR / "courses" / course_id / "notes"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{topic_slug}{ext}"
        output_path.write_text(resp.content, encoding="utf-8")

        return SkillResult(
            success=True,
            output_path=str(output_path),
            data={
                "content": resp.content,
                "format": note_format,
                "topic": topic or "Full Course",
                "sources_used": len(results),
                "model": resp.model,
            },
        )


def _slugify(text: str) -> str:
    import re
    slug = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[\s]+", "_", slug).strip("_")[:60]
