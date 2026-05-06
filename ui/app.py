"""Main Gradio application for nano-NOTEBOOKLM."""

from __future__ import annotations

import logging

import gradio as gr

from nano_notebooklm.ai.router import ModelRouter
from nano_notebooklm.kb.store import KBStore
from nano_notebooklm.orchestrator.engine import Orchestrator

logger = logging.getLogger(__name__)

CSS = """
.gradio-container { max-width: 1400px; margin: auto; }
footer { display: none !important; }
"""


def create_app() -> gr.Blocks:
    """Create the main Gradio application with all tabs."""

    # Initialize core components
    kb = KBStore()
    router = ModelRouter()
    orchestrator = Orchestrator(kb, router)

    with gr.Blocks(title="nano-NOTEBOOKLM") as app:
        gr.Markdown(
            "# nano-NOTEBOOKLM\n"
            "AI-powered study assistant — upload course materials, ask questions, "
            "explore knowledge graphs, generate notes, and prepare for exams."
        )

        # Show backend status
        backends = list(router.backends.keys())
        courses = orchestrator.list_courses()
        status_parts = [
            f"AI Backends: {', '.join(backends) if backends else 'None (set API keys in .env)'}",
            f"Courses loaded: {', '.join(courses) if courses else 'None (upload materials first)'}",
        ]
        gr.Markdown(f"*{' | '.join(status_parts)}*")

        # Create all tabs
        from ui.tabs.upload_tab import create_upload_tab
        from ui.tabs.qa_tab import create_qa_tab
        from ui.tabs.kg_tab import create_kg_tab
        from ui.tabs.notes_tab import create_notes_tab
        from ui.tabs.exam_tab import create_exam_tab
        from ui.tabs.mastery_tab import create_mastery_tab

        create_upload_tab(kb)
        create_qa_tab(orchestrator)
        create_kg_tab(orchestrator)
        create_notes_tab(orchestrator)
        create_exam_tab(orchestrator)
        create_mastery_tab(orchestrator)

    return app


def main():
    """Launch the application."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    app = create_app()
    app.launch(
        server_name="0.0.0.0",
        server_port=7860,
        share=False,
    )


if __name__ == "__main__":
    main()
