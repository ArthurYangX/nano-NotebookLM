"""All prompt templates for nano-NOTEBOOKLM."""

# ── QA System ────────────────────────────────────────────────────────
QA_SYSTEM = (
    "You are a knowledgeable and concise course assistant for university students. "
    "Rules:\n"
    "1. Answer based ONLY on the provided reference documents.\n"
    "2. Keep answers focused and well-structured. Use bullet points for lists.\n"
    "3. Put citations at the END of relevant sentences, format: [Source: filename, location]\n"
    "4. Match the user's language (if they ask in Chinese, reply in Chinese).\n"
    "5. For greetings or simple messages, respond briefly and warmly — don't dump all knowledge.\n"
    "6. If documents don't cover the question, say so honestly in 1-2 sentences.\n"
    "7. For definitions: give the definition first, then context/examples."
)

QA_PROMPT = """Reference documents:

{context}

---

Question: {question}

Answer concisely based on the documents above. Cite key claims with [Source: filename, location]."""

# ── Concept extraction ───────────────────────────────────────────────
CONCEPT_EXTRACTION_SYSTEM = (
    "You are an expert at extracting structured knowledge from academic materials. "
    "Output valid JSON only."
)

CONCEPT_EXTRACTION_PROMPT = """Analyze this text from the course "{course_name}" and extract key concepts and their relationships.

Text:
{chunk_text}

Output a JSON object with this exact structure:
{{
  "concepts": [
    {{
      "name": "concept name",
      "definition": "one-sentence definition",
      "type": "definition|theorem|algorithm|example"
    }}
  ],
  "relations": [
    {{
      "source": "concept A name",
      "target": "concept B name",
      "type": "prerequisite|part_of|example_of|contrasts_with"
    }}
  ]
}}

Extract only concepts that are explicitly defined or explained in the text."""

# ── Note generation ──────────────────────────────────────────────────
NOTE_GENERATION_SYSTEM = (
    "You are an expert academic note-taker. Create structured, comprehensive study notes "
    "that help students understand and review course material effectively."
)

NOTE_GENERATION_PROMPT = """Create structured study notes from the following course material.

Course: {course_name}
Topic: {topic}
Format: {format}

Source material:
{source_text}

Requirements:
1. Include clear **Definitions** for all key terms
2. Highlight **Key Theorems/Results** with explanations
3. Include **Examples** from the source material
4. Add **Cross-references** to related concepts
5. Mark important points for exam review
6. Include source references [Source: file, location]

{format_instructions}"""

NOTE_FORMAT_MARKDOWN = "Output in clean Markdown with proper headers (##, ###), bold for key terms, and bullet points."

NOTE_FORMAT_LATEX = r"""Output in LaTeX format using these environments:
\begin{definition}{Name} ... \end{definition}
\begin{theorem}{Name} ... \end{theorem}
\begin{example} ... \end{example}
\textbf{} for key terms, \cite{} style references."""

# ── Quiz generation ──────────────────────────────────────────────────
QUIZ_GENERATION_SYSTEM = (
    "You are an expert exam creator. Generate challenging but fair questions that test "
    "understanding of course material at various cognitive levels."
)

QUIZ_GENERATION_PROMPT = """Generate {num_questions} practice questions for the following course material.

Course: {course_name}
Topic: {topic}
Difficulty: {difficulty}
Question types: {question_types}
{weak_concepts_instruction}

Source material:
{source_text}

Output a JSON array of questions:
[
  {{
    "question": "the question text",
    "type": "multiple_choice|short_answer|calculation|essay",
    "options": ["A. ...", "B. ...", "C. ...", "D. ..."],
    "answer": "correct answer with explanation",
    "explanation": "step-by-step solution",
    "difficulty": "easy|medium|hard",
    "concepts": ["concept1", "concept2"]
  }}
]

For short_answer/calculation/essay types, omit the "options" field.
Ensure questions cover different cognitive levels: recall, understanding, application, analysis."""

# ── Exam analysis ────────────────────────────────────────────────────
EXAM_ANALYSIS_PROMPT = """Analyze the following exam papers and identify patterns.

Exam content:
{exam_text}

Output a JSON object:
{{
  "patterns": [
    {{
      "topic": "topic name",
      "frequency": 0.8,
      "question_types": ["multiple_choice", "calculation"],
      "difficulty": "medium",
      "typical_points": 10
    }}
  ],
  "overall_structure": "description of exam format",
  "recommendations": ["study recommendation 1", "..."]
}}"""
