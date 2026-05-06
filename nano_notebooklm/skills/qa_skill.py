"""Interactive Q&A skill using RAG (Retrieval-Augmented Generation)."""

from __future__ import annotations

from nano_notebooklm.ai import prompt_templates as prompts
from nano_notebooklm.orchestrator.memory import add_interaction, get_context_prompt
from nano_notebooklm.skills.base import Skill
from nano_notebooklm.types import SkillResult


class QASkill(Skill):
    name = "qa"
    description = "Answer questions using course materials with source citations"

    async def execute(self, params: dict) -> SkillResult:
        question = params.get("question", "")
        course_filter = params.get("course_filter")
        top_k = params.get("top_k", 5)
        checked_files = params.get("checked_files")  # Source files user has checked

        if not question:
            return SkillResult(success=False, error="No question provided")

        # 1. Retrieve relevant chunks
        results = self.kb.search(question, top_k=top_k, course_id=course_filter)

        # Filter by checked source files if specified
        if checked_files:
            results = [r for r in results if r.source_file in checked_files]
            # If too few results after filtering, search again with more
            if len(results) < 2:
                more = self.kb.search(question, top_k=top_k * 3, course_id=course_filter)
                filtered = [r for r in more if r.source_file in checked_files]
                if filtered:
                    results = filtered[:top_k]

        if not results:
            return SkillResult(
                success=True,
                data={
                    "answer": "No relevant content found in the selected sources. Try checking more sources in the Library panel, or upload additional materials.",
                    "sources": [],
                },
            )

        # 2. Build context
        context = "\n\n---\n\n".join(
            f"[Source: {r.source_file}, {r.location}]\n{r.text}"
            for r in results
        )

        # 3. Get user memory context
        memory_context = get_context_prompt(course_filter)
        system = prompts.QA_SYSTEM
        if memory_context:
            system += f"\n\nStudent context:\n{memory_context}"

        # 4. Generate answer
        prompt = prompts.QA_PROMPT.format(context=context, question=question)
        resp = await self.router.complete(
            prompt, task_type="qa_answer", system=system, temperature=0.3,
        )

        # 5. Record interaction in memory
        add_interaction(
            course_id=course_filter or "general",
            question=question,
            summary=resp.content[:200],
        )

        return SkillResult(
            success=True,
            data={
                "answer": resp.content,
                "sources": [
                    {
                        "chunk_id": r.chunk_id,
                        "text": r.text[:200] + "..." if len(r.text) > 200 else r.text,
                        "source_file": r.source_file,
                        "location": r.location,
                        "score": r.score,
                    }
                    for r in results
                ],
                "model": resp.model,
                "tokens_used": resp.input_tokens + resp.output_tokens,
            },
        )
