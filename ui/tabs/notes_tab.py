"""Note generation tab."""

from __future__ import annotations

import asyncio
import logging

import gradio as gr

from nano_notebooklm.orchestrator.engine import Orchestrator

logger = logging.getLogger(__name__)


def create_notes_tab(orchestrator: Orchestrator):
    """Create the notes generation tab."""

    with gr.Tab("Notes"):
        gr.Markdown("## Study Notes Generator")
        gr.Markdown("Generate structured study notes from course materials in Markdown or LaTeX.")

        with gr.Row():
            with gr.Column(scale=1):
                notes_course = gr.Dropdown(
                    label="Course",
                    choices=orchestrator.list_courses(),
                    value=orchestrator.list_courses()[0] if orchestrator.list_courses() else None,
                )
                notes_topic = gr.Textbox(
                    label="Topic (optional)",
                    placeholder="Leave empty for full course overview",
                )
                notes_format = gr.Radio(
                    label="Output Format",
                    choices=["markdown", "latex"],
                    value="markdown",
                )
                generate_btn = gr.Button("Generate Notes", variant="primary")
                notes_status = gr.Textbox(label="Status", lines=2, interactive=False)

            with gr.Column(scale=2):
                notes_output = gr.Markdown(
                    value="*Click 'Generate Notes' to create study notes.*",
                    label="Generated Notes",
                )

        with gr.Accordion("Raw Output / LaTeX Source", open=False):
            raw_output = gr.Code(label="Raw Content", language="markdown")

        def handle_generate(course_id, topic, fmt):
            if not course_id:
                return "Select a course first.", "", ""

            try:
                loop = asyncio.new_event_loop()
                result = loop.run_until_complete(
                    orchestrator.run_skill("note_generator", {
                        "course_id": course_id,
                        "topic": topic if topic else None,
                        "format": fmt,
                    })
                )
                loop.close()

                if not result.success:
                    return f"Error: {result.error}", "", ""

                content = result.data.get("content", "")
                path = result.output_path or ""
                sources = result.data.get("sources_used", 0)
                status = f"Notes generated! {sources} sources used. Saved to: {path}"

                if fmt == "latex":
                    return status, f"```latex\n{content}\n```", content
                return status, content, content

            except Exception as e:
                logger.exception("Note generation failed")
                return f"Error: {e}", "", ""

        generate_btn.click(
            handle_generate,
            inputs=[notes_course, notes_topic, notes_format],
            outputs=[notes_status, notes_output, raw_output],
        )
