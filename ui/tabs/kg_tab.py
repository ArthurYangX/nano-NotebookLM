"""Knowledge graph visualization tab."""

from __future__ import annotations

import asyncio
import logging

import gradio as gr

from nano_notebooklm.ai.router import ModelRouter
from nano_notebooklm.kb.store import KBStore
from nano_notebooklm.kg.extractor import extract_from_chunks
from nano_notebooklm.kg.graph import KnowledgeGraph
from nano_notebooklm.kg.merger import merge_concepts, merge_relations
from nano_notebooklm.kg.visualizer import to_mermaid
from nano_notebooklm.orchestrator.engine import Orchestrator

logger = logging.getLogger(__name__)


def create_kg_tab(orchestrator: Orchestrator):
    """Create the knowledge graph tab."""

    with gr.Tab("Knowledge Graph"):
        gr.Markdown("## Knowledge Graph Explorer")
        gr.Markdown("Extract and visualize concept relationships from course materials.")

        with gr.Row():
            kg_course = gr.Dropdown(
                label="Course",
                choices=orchestrator.list_courses(),
                value=orchestrator.list_courses()[0] if orchestrator.list_courses() else None,
            )
            max_concepts = gr.Slider(label="Max Concepts", minimum=10, maximum=100, value=30, step=5)
            build_kg_btn = gr.Button("Build Knowledge Graph", variant="primary")

        kg_status = gr.Textbox(label="Status", lines=2, interactive=False)

        with gr.Row():
            with gr.Column(scale=2):
                mermaid_display = gr.Markdown(
                    value="*Click 'Build Knowledge Graph' to generate.*",
                    label="Knowledge Graph (Mermaid)",
                )
            with gr.Column(scale=1):
                concept_search = gr.Textbox(label="Search Concepts", placeholder="e.g. convolution")
                search_results = gr.Markdown(value="", label="Search Results")

        concept_list = gr.Dataframe(
            headers=["Concept", "Type", "Definition", "Sources"],
            label="Extracted Concepts",
            interactive=False,
        )

        def handle_build_kg(course_id, max_n):
            if not course_id:
                return "Select a course first.", "", [], ""

            try:
                chunks = orchestrator.kb.get_chunks(course_id)
                if not chunks:
                    return "No chunks found for this course.", "", [], ""

                loop = asyncio.new_event_loop()
                concepts, relations = loop.run_until_complete(
                    extract_from_chunks(chunks, course_id, orchestrator.router, max_chunks=max_n)
                )
                loop.close()

                # Merge duplicates
                concepts = merge_concepts(concepts)
                relations = merge_relations(relations)

                # Build graph
                kg = KnowledgeGraph()
                kg.add_concepts(concepts)
                kg.add_relations(relations)

                # Save
                from nano_notebooklm import config
                kg_path = config.ARTIFACTS_DIR / "courses" / course_id / "knowledge_graph.json"
                kg.save(kg_path)

                # Generate Mermaid
                mermaid_code = to_mermaid(kg, course_id, max_nodes=50)
                mermaid_md = f"```mermaid\n{mermaid_code}\n```"

                stats = kg.stats()
                status = f"Built KG: {stats['nodes']} concepts, {stats['edges']} relations"

                # Build concept table
                table_data = []
                for c in concepts[:50]:
                    table_data.append([
                        c.name,
                        c.concept_type,
                        c.definition[:100] + "..." if len(c.definition) > 100 else c.definition,
                        str(len(c.chunk_ids)),
                    ])

                return status, mermaid_md, table_data, ""
            except Exception as e:
                logger.exception("KG build failed")
                return f"Error: {e}", "", [], ""

        def handle_search(query):
            if not query:
                return ""
            # Load KG and search
            from nano_notebooklm import config
            kg = KnowledgeGraph()
            # Try all courses
            courses_dir = config.ARTIFACTS_DIR / "courses"
            if courses_dir.exists():
                for d in courses_dir.iterdir():
                    kg_path = d / "knowledge_graph.json"
                    if kg_path.exists():
                        kg.load(kg_path)

            results = kg.search_concepts(query)
            if not results:
                return "*No concepts found.*"

            lines = []
            for r in results[:10]:
                lines.append(f"**{r.get('name', '')}** ({r.get('concept_type', '')})")
                lines.append(f"> {r.get('definition', 'No definition')}\n")
            return "\n".join(lines)

        build_kg_btn.click(
            handle_build_kg,
            inputs=[kg_course, max_concepts],
            outputs=[kg_status, mermaid_display, concept_list, search_results],
        )
        concept_search.submit(handle_search, inputs=[concept_search], outputs=[search_results])
