"""Q&A tab with RAG-powered answers and source citations."""

from __future__ import annotations

import asyncio
import logging

import gradio as gr

from nano_notebooklm.orchestrator.engine import Orchestrator

logger = logging.getLogger(__name__)


def create_qa_tab(orchestrator: Orchestrator):
    """Create the Q&A tab."""

    with gr.Tab("Q&A"):
        gr.Markdown("## Course Knowledge Q&A")
        gr.Markdown("Ask questions about your course materials. Answers include source citations.")

        with gr.Row():
            with gr.Column(scale=3):
                chatbot = gr.Chatbot(
                    label="Conversation",
                    height=500,
                )
                with gr.Row():
                    question = gr.Textbox(
                        label="Your Question",
                        placeholder="e.g. What is backpropagation? / 什么是卷积？",
                        scale=4,
                    )
                    send_btn = gr.Button("Send", variant="primary", scale=1)

            with gr.Column(scale=1):
                course_filter = gr.Dropdown(
                    label="Filter by Course",
                    choices=["All Courses"] + orchestrator.list_courses(),
                    value="All Courses",
                )
                gr.Markdown("### Sources")
                sources_display = gr.Markdown(
                    value="*Sources will appear here after asking a question.*",
                    label="References",
                )

        # ── Handlers ─────────────────────────────────────────────
        def handle_question(message, history, course):
            if not message.strip():
                return history, "", "*No question asked.*"

            course_id = None if course == "All Courses" else course

            # Run async in sync context
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(
                    orchestrator.handle(message, course_filter=course_id)
                )
            finally:
                loop.close()

            if not result.success:
                answer = f"Error: {result.error}"
                sources_md = ""
            else:
                answer = result.data.get("answer", "No answer generated.")
                sources = result.data.get("sources", [])
                sources_md = _format_sources(sources)

            history = history or []
            history.append({"role": "user", "content": message})
            history.append({"role": "assistant", "content": answer})

            return history, "", sources_md

        send_btn.click(
            handle_question,
            inputs=[question, chatbot, course_filter],
            outputs=[chatbot, question, sources_display],
        )
        question.submit(
            handle_question,
            inputs=[question, chatbot, course_filter],
            outputs=[chatbot, question, sources_display],
        )


def _format_sources(sources: list[dict]) -> str:
    """Format source citations as Markdown."""
    if not sources:
        return "*No sources found.*"

    lines = ["### Referenced Sources\n"]
    for i, src in enumerate(sources, 1):
        score = src.get("score", 0)
        lines.append(
            f"**[{i}]** `{src['source_file']}` — {src['location']}\n"
            f"> {src['text']}\n"
            f"*Relevance: {score:.3f}*\n"
        )
    return "\n".join(lines)
