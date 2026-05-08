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

    def prepare_inputs(self, params: dict) -> dict | None:
        """Build the LLM inputs without invoking it. Returns None if the
        precondition fails (e.g. no chunks). Round 2 #5 uses this so the
        streaming endpoint can reuse the same prompt and pipe deltas
        directly through `router.complete_stream` instead of running the
        whole skill then chunking the result.
        """
        course_id = params.get("course_id", "")
        topic = params.get("topic")
        note_format = params.get("format", "markdown")
        user_lang = params.get("user_lang")

        if not course_id:
            return None

        if topic:
            results = self.kb.search(topic, top_k=15, course_id=course_id)
        else:
            chunks = self.kb.get_chunks(course_id)
            if not chunks:
                return None
            results = self.kb.search(
                f"key concepts definitions theorems examples for {course_id}",
                top_k=20,
                course_id=course_id,
            )
        if not results:
            return None

        source_text = "\n\n---\n\n".join(
            f"[Source: {r.source_file}, {r.location}]\n{r.text}"
            for r in results
        )
        format_instructions = (prompts.NOTE_FORMAT_LATEX
                               if note_format == "latex"
                               else prompts.NOTE_FORMAT_MARKDOWN)
        prompt = prompts.NOTE_GENERATION_PROMPT.format(
            course_name=course_id,
            topic=topic or "Full Course Overview",
            format=note_format,
            source_text=source_text,
            format_instructions=format_instructions,
        )
        system = prompts.NOTE_GENERATION_SYSTEM
        binding = prompts.USER_LANG_BINDING(user_lang)
        if binding:
            system = f"{system}\n\n{binding}"
        return {
            "prompt": prompt,
            "system": system,
            "task_type": "note_generation",
            "temperature": 0.3,
            "max_tokens": 8192,
            "format": note_format,
            "topic": topic or "Full Course",
            "sources_used": len(results),
            "course_id": course_id,
        }

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

        prepared = self.prepare_inputs(params)
        if prepared is None:
            if not course_id:
                return SkillResult(success=False, error="No course_id provided")
            chunks = self.kb.get_chunks(course_id)
            if not chunks:
                return SkillResult(success=False, error=f"No chunks found for course {course_id}")
            return SkillResult(success=False, error="No relevant content found")

        resp = await self.router.complete(
            prepared["prompt"],
            task_type=prepared["task_type"],
            system=prepared["system"],
            temperature=prepared["temperature"],
            max_tokens=prepared["max_tokens"],
        )

        # 5. Save to file. Defense-in-depth: even though all callers (the API
        # endpoint and the agent's generate_note tool) now whitelist
        # course_id against orchestrator.list_courses(), we re-assert the
        # resolved output path stays under ARTIFACTS_DIR/courses/ — `course_id`
        # values like "../../etc" would otherwise let any future caller
        # write outside the artifacts tree.
        ext = ".tex" if note_format == "latex" else ".md"
        topic_slug = _slugify(topic) if topic else "full_course"
        output_dir = (config.ARTIFACTS_DIR / "courses" / course_id / "notes").resolve()
        allowed_root = (config.ARTIFACTS_DIR / "courses").resolve()
        if not output_dir.is_relative_to(allowed_root):
            return SkillResult(success=False,
                               error=f"course_id {course_id!r} resolves outside artifacts root")
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
                "sources_used": prepared["sources_used"],
                "model": resp.model,
            },
        )


def _slugify(text: str) -> str:
    import re
    slug = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[\s]+", "_", slug).strip("_")[:60]
