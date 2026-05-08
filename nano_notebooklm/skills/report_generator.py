"""Course report generation skill — summary, analysis, code, PPT outline."""

from __future__ import annotations

import logging
from pathlib import Path

from nano_notebooklm.ai import prompt_templates as prompts
from nano_notebooklm import config
from nano_notebooklm.skills.base import Skill
from nano_notebooklm.types import SkillResult

logger = logging.getLogger(__name__)

REPORT_SYSTEM = (
    "You are an expert academic report writer. Create professional, well-structured "
    "reports suitable for course submissions. Include proper sections, analysis, "
    "and conclusions. Match the language of the source material."
)

REPORT_PROMPT = """Write a comprehensive course report based on the following materials.

Course: {course_name}
Report Type: {report_type}
Include Code Examples: {include_code}

Source Materials:
{source_text}

Requirements:
1. Title page with course name and report title
2. Table of contents
3. Introduction with objectives
4. Main body organized by topics
5. Key findings and analysis
6. Conclusion and future directions
7. References to source materials

{code_instructions}

Output the report in {format} format."""

CODE_INSTRUCTIONS = """For code examples:
- Include relevant code snippets with explanations
- Add comments explaining key logic
- Show sample outputs where appropriate"""

PPT_OUTLINE_PROMPT = """Based on this report content, create a presentation outline.

Report:
{report_content}

Create a structured slide outline with:
- Title slide
- Agenda/Overview
- 8-12 content slides with bullet points
- Summary/Conclusion slide
- Q&A slide

Output as Markdown with ## for each slide title and bullet points for content."""


class ReportGeneratorSkill(Skill):
    name = "report_generator"
    description = "Generate course reports with optional code and PPT outline"

    def prepare_inputs(self, params: dict) -> dict | None:
        """Round 2 #5: build the LLM inputs without invoking it so the
        streaming endpoint can pipe deltas via router.complete_stream."""
        course_id = params.get("course_id", "")
        report_type = params.get("report_type", "summary")
        include_code = params.get("include_code", False)
        fmt = params.get("format", "markdown")
        if not course_id:
            return None
        user_lang = params.get("user_lang")
        results = self.kb.search(
            f"key concepts summary overview {course_id}",
            top_k=20,
            course_id=course_id,
        )
        if not results:
            return None
        source_text = "\n\n---\n\n".join(
            f"[Source: {r.source_file}, {r.location}]\n{r.text}"
            for r in results
        )
        prompt = REPORT_PROMPT.format(
            course_name=course_id,
            report_type=report_type,
            include_code="Yes" if include_code else "No",
            source_text=source_text,
            code_instructions=CODE_INSTRUCTIONS if include_code else "",
            format=fmt,
        )
        system = REPORT_SYSTEM
        binding = prompts.USER_LANG_BINDING(user_lang)
        if binding:
            system = f"{system}\n\n{binding}"
        return {
            "prompt": prompt,
            "system": system,
            "task_type": "report_writing",
            "temperature": 0.3,
            "max_tokens": 8192,
            "course_id": course_id,
            "report_type": report_type,
            "format": fmt,
            "sources_used": len(results),
        }

    async def execute(self, params: dict) -> SkillResult:
        """
        Params:
            course_id (str): Course identifier
            report_type (str): "summary" | "analysis" | "code_walkthrough"
            include_code (bool): Whether to include code examples
            format (str): "markdown" | "latex"
        """
        course_id = params.get("course_id", "")
        report_type = params.get("report_type", "summary")
        include_code = params.get("include_code", False)
        fmt = params.get("format", "markdown")

        prepared = self.prepare_inputs(params)
        if prepared is None:
            if not course_id:
                return SkillResult(success=False, error="No course_id provided")
            return SkillResult(success=False, error="No content found")

        prompt = prepared["prompt"]

        resp = await self.router.complete(
            prompt,
            task_type="report_writing",
            system=prepared["system"],
            temperature=0.3,
            max_tokens=8192,
        )

        # 3. Save report
        ext = ".tex" if fmt == "latex" else ".md"
        output_dir = config.ARTIFACTS_DIR / "courses" / course_id / "reports"
        output_dir.mkdir(parents=True, exist_ok=True)
        report_path = output_dir / f"report_{report_type}{ext}"
        report_path.write_text(resp.content, encoding="utf-8")

        # 4. Generate PPT outline
        ppt_prompt = PPT_OUTLINE_PROMPT.format(report_content=resp.content[:4000])
        ppt_resp = await self.router.complete(
            ppt_prompt,
            task_type="report_writing",
            system="You create concise presentation outlines.",
            temperature=0.3,
            max_tokens=2048,
        )

        ppt_path = output_dir / f"slides_outline_{report_type}.md"
        ppt_path.write_text(ppt_resp.content, encoding="utf-8")

        return SkillResult(
            success=True,
            output_path=str(report_path),
            data={
                "report_path": str(report_path),
                "ppt_outline_path": str(ppt_path),
                "report_type": report_type,
                "format": fmt,
                # review-swarm fix-all v3 #C8: `results` was a local of
                # prepare_inputs and is undefined here — call site raised
                # NameError after writing both files. Use the count carried
                # over via prepared.
                "sources_used": prepared["sources_used"],
            },
        )
