"""Exam preparation tab — quiz generation and interactive testing."""

from __future__ import annotations

import asyncio
import json
import logging

import gradio as gr

from nano_notebooklm.orchestrator.engine import Orchestrator

logger = logging.getLogger(__name__)


def create_exam_tab(orchestrator: Orchestrator):
    """Create the exam preparation tab."""

    with gr.Tab("Exam Prep"):
        gr.Markdown("## Exam Preparation")
        gr.Markdown("Generate practice tests and analyze exam patterns.")

        with gr.Tabs():
            # ── Quiz Generation ──
            with gr.Tab("Generate Quiz"):
                with gr.Row():
                    with gr.Column(scale=1):
                        quiz_course = gr.Dropdown(
                            label="Course",
                            choices=orchestrator.list_courses(),
                        )
                        quiz_topic = gr.Textbox(label="Topic (optional)", placeholder="e.g. Neural Networks")
                        quiz_num = gr.Slider(label="Number of Questions", minimum=3, maximum=20, value=5, step=1)
                        quiz_difficulty = gr.Radio(
                            label="Difficulty", choices=["easy", "medium", "hard"], value="medium"
                        )
                        quiz_btn = gr.Button("Generate Quiz", variant="primary")
                        quiz_status = gr.Textbox(label="Status", lines=2, interactive=False)

                    with gr.Column(scale=2):
                        quiz_display = gr.Markdown(value="*Generate a quiz to start practicing.*")

                with gr.Accordion("Show Answers", open=False):
                    answers_display = gr.Markdown(value="")

            # ── Exam Analysis ──
            with gr.Tab("Exam Analysis"):
                with gr.Row():
                    analysis_course = gr.Dropdown(
                        label="Course",
                        choices=orchestrator.list_courses(),
                    )
                    analyze_btn = gr.Button("Analyze Exam Patterns", variant="primary")

                analysis_output = gr.Markdown(value="*Select a course and click analyze.*")

        # ── Handlers ──
        def handle_quiz(course_id, topic, num, difficulty):
            if not course_id:
                return "Select a course.", "", ""

            try:
                loop = asyncio.new_event_loop()
                result = loop.run_until_complete(
                    orchestrator.run_skill("quiz_generator", {
                        "course_id": course_id,
                        "topic": topic if topic else None,
                        "num_questions": int(num),
                        "difficulty": difficulty,
                    })
                )
                loop.close()

                if not result.success:
                    return f"Error: {result.error}", "", ""

                questions = result.data.get("quiz", [])
                status = f"Generated {len(questions)} questions"

                # Format questions (without answers)
                q_lines = ["## Practice Quiz\n"]
                a_lines = ["## Answers\n"]

                for i, q in enumerate(questions, 1):
                    q_text = q.get("question", "")
                    q_lines.append(f"**Q{i}.** {q_text}\n")

                    options = q.get("options", [])
                    if options:
                        for opt in options:
                            q_lines.append(f"  {opt}\n")

                    q_lines.append("")

                    # Answers
                    a_lines.append(f"**Q{i}.** {q.get('answer', 'N/A')}\n")
                    explanation = q.get("explanation", "")
                    if explanation:
                        a_lines.append(f"*Explanation: {explanation}*\n")
                    a_lines.append("")

                return status, "\n".join(q_lines), "\n".join(a_lines)

            except Exception as e:
                logger.exception("Quiz generation failed")
                return f"Error: {e}", "", ""

        def handle_analysis(course_id):
            if not course_id:
                return "*Select a course.*"

            try:
                loop = asyncio.new_event_loop()
                result = loop.run_until_complete(
                    orchestrator.run_skill("exam_analyzer", {"course_id": course_id})
                )
                loop.close()

                if not result.success:
                    return f"Error: {result.error}"

                data = result.data
                lines = ["## Exam Pattern Analysis\n"]

                patterns = data.get("patterns", [])
                if patterns:
                    lines.append("### Topic Frequency\n")
                    lines.append("| Topic | Frequency | Question Types | Difficulty |")
                    lines.append("|-------|-----------|---------------|------------|")
                    for p in patterns:
                        freq = p.get("frequency", 0)
                        types = ", ".join(p.get("question_types", []))
                        lines.append(f"| {p.get('topic', '')} | {freq:.0%} | {types} | {p.get('difficulty', '')} |")

                structure = data.get("overall_structure", "")
                if structure:
                    lines.append(f"\n### Exam Structure\n{structure}\n")

                recs = data.get("recommendations", [])
                if recs:
                    lines.append("### Study Recommendations\n")
                    for r in recs:
                        lines.append(f"- {r}")

                return "\n".join(lines)

            except Exception as e:
                logger.exception("Analysis failed")
                return f"Error: {e}"

        quiz_btn.click(
            handle_quiz,
            inputs=[quiz_course, quiz_topic, quiz_num, quiz_difficulty],
            outputs=[quiz_status, quiz_display, answers_display],
        )
        analyze_btn.click(handle_analysis, inputs=[analysis_course], outputs=[analysis_output])
