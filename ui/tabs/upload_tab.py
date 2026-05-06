"""Document upload and course ingestion tab."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import gradio as gr

from nano_notebooklm import config
from nano_notebooklm.kb.store import KBStore

logger = logging.getLogger(__name__)


def create_upload_tab(kb: KBStore):
    """Create the document upload tab."""

    with gr.Tab("Upload"):
        gr.Markdown("## Upload Course Materials")
        gr.Markdown("Upload PDF, PPTX, DOCX, or Markdown files to build the knowledge base.")

        with gr.Row():
            with gr.Column(scale=2):
                course_name = gr.Textbox(
                    label="Course Name",
                    placeholder="e.g. CS231N, 模式识别",
                    info="Used as folder name for organizing materials",
                )
                files = gr.File(
                    label="Upload Files",
                    file_count="multiple",
                    file_types=[".pdf", ".pptx", ".docx", ".md", ".txt"],
                )
                upload_btn = gr.Button("Upload & Index", variant="primary")

            with gr.Column(scale=1):
                status = gr.Textbox(label="Status", lines=8, interactive=False)

        gr.Markdown("---")
        gr.Markdown("### Or ingest from existing directory")

        with gr.Row():
            dir_path = gr.Textbox(
                label="Course Directory Path",
                placeholder=str(config.COURSE_DATA_DIR),
            )
            dir_course_name = gr.Textbox(
                label="Course Name (optional)",
                placeholder="Auto-detected from directory name",
            )
            ingest_btn = gr.Button("Ingest Directory")

        dir_status = gr.Textbox(label="Ingest Status", lines=5, interactive=False)

        # ── Handlers ─────────────────────────────────────────────
        def handle_upload(course_id, file_list):
            if not course_id:
                return "Please enter a course name."
            if not file_list:
                return "No files selected."

            try:
                # Create course directory and copy files
                course_dir = config.ARTIFACTS_DIR / "uploads" / course_id
                course_dir.mkdir(parents=True, exist_ok=True)

                for f in file_list:
                    dest = course_dir / Path(f.name).name
                    shutil.copy2(f.name, dest)

                # Ingest
                course = kb.ingest_course(course_dir, course_id)
                kb.build_index(course_id)

                chunks = kb.get_chunks(course_id)
                return (
                    f"Success!\n"
                    f"Course: {course_id}\n"
                    f"Files: {len(file_list)}\n"
                    f"Chunks: {len(chunks)}\n"
                    f"Index built and ready for search."
                )
            except Exception as e:
                logger.exception("Upload failed")
                return f"Error: {e}"

        def handle_ingest(dir_path_str, course_id):
            if not dir_path_str:
                return "Please enter a directory path."

            dir_path = Path(dir_path_str)
            if not dir_path.exists():
                return f"Directory not found: {dir_path}"

            try:
                cid = course_id if course_id else dir_path.name
                course = kb.ingest_course(dir_path, cid)
                kb.build_index(cid)

                chunks = kb.get_chunks(cid)
                return (
                    f"Success!\n"
                    f"Course: {cid}\n"
                    f"Chunks: {len(chunks)}\n"
                    f"Index built and ready."
                )
            except Exception as e:
                logger.exception("Ingest failed")
                return f"Error: {e}"

        upload_btn.click(handle_upload, inputs=[course_name, files], outputs=[status])
        ingest_btn.click(handle_ingest, inputs=[dir_path, dir_course_name], outputs=[dir_status])
