"""Knowledge mastery tracking dashboard tab."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import gradio as gr

from nano_notebooklm import config
from nano_notebooklm.orchestrator.engine import Orchestrator

logger = logging.getLogger(__name__)


def create_mastery_tab(orchestrator: Orchestrator):
    """Create the mastery tracking dashboard tab."""

    with gr.Tab("Mastery"):
        gr.Markdown("## Knowledge Mastery Dashboard")
        gr.Markdown("Track your understanding of course concepts based on quiz performance.")

        with gr.Row():
            mastery_course = gr.Dropdown(
                label="Course",
                choices=orchestrator.list_courses(),
            )
            refresh_btn = gr.Button("Refresh Dashboard", variant="primary")

        with gr.Row():
            with gr.Column(scale=1):
                overall_score = gr.Textbox(label="Overall Mastery", interactive=False)
                total_concepts = gr.Textbox(label="Concepts Tracked", interactive=False)

            with gr.Column(scale=2):
                weak_areas = gr.Markdown(value="*Take a quiz first to see mastery data.*")

        mastery_table = gr.Dataframe(
            headers=["Concept", "Score", "Attempts", "Status"],
            label="Concept Mastery Details",
            interactive=False,
        )

        def handle_refresh(course_id):
            if not course_id:
                return "", "", "*Select a course.*", []

            mastery_path = config.ARTIFACTS_DIR / "courses" / course_id / "mastery.json"
            if not mastery_path.exists():
                return "N/A", "0", "*No mastery data yet. Take a quiz first!*", []

            mastery = json.loads(mastery_path.read_text())

            if not mastery:
                return "N/A", "0", "*No mastery data yet.*", []

            # Overall score
            scores = [v["score"] for v in mastery.values()]
            avg = sum(scores) / len(scores)
            overall = f"{avg:.0%}"

            # Weak areas
            weak = sorted(mastery.values(), key=lambda x: x["score"])
            weak_lines = ["### Weak Areas (Need Review)\n"]
            for w in weak[:10]:
                score = w["score"]
                emoji = "🔴" if score < 0.3 else "🟡" if score < 0.6 else "🟢"
                weak_lines.append(f"- {emoji} **{w['concept']}** — {score:.0%} ({w['attempts']} attempts)")

            # Table
            table_data = []
            for v in sorted(mastery.values(), key=lambda x: x["score"]):
                score = v["score"]
                status = "Needs Review" if score < 0.3 else "Learning" if score < 0.6 else "Good" if score < 0.8 else "Mastered"
                table_data.append([
                    v["concept"],
                    f"{score:.0%}",
                    str(v["attempts"]),
                    status,
                ])

            return overall, str(len(mastery)), "\n".join(weak_lines), table_data

        refresh_btn.click(
            handle_refresh,
            inputs=[mastery_course],
            outputs=[overall_score, total_concepts, weak_areas, mastery_table],
        )
