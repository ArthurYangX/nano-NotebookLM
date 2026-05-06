/* global React, API */
// Data layer — loads real data from API, falls back to mock for demo

const SAMPLE_SOURCES = [
  { id: "s1", type: "pdf", title: "Loading sources...", meta: "connecting to backend", active: true, checked: true, collection: "main" },
];

// Will be dynamically updated when courses load
let SAMPLE_COLLECTIONS = [
  { id: "main", name: "All Courses", count: 0, color: "oklch(0.42 0.08 160)" },
];

const READER_DOC = {
  chapter: "nano-NOTEBOOKLM",
  title: "Welcome to nano-NOTEBOOKLM",
  sub: "Upload course materials or select a course to begin exploring.",
  body: [
    { kind: "p", text: "Use the Library panel on the left to manage your sources. Upload PDF, PPTX, DOCX, or Markdown files to build your knowledge base." },
    { kind: "h2", num: "1.1", text: "Getting Started" },
    { kind: "p", text: "1. Upload course materials via the Library panel. 2. Ask questions in the Assistant sidebar. 3. Generate notes, mind maps, and quizzes from the tabs above." },
    { kind: "h2", num: "1.2", text: "Supported Features" },
    { kind: "p", text: "Reader — browse extracted content with interactive highlights. Notes — generate structured study notes in Outline, Cornell, or Card format. Mind Map — visualize knowledge relationships. Quiz — auto-generated practice tests with AI grading." },
  ]
};

const NOTES_DATA = {
  title: "Study Notes",
  generated: "Click 'Generate Notes' in the Assistant to create notes from your sources.",
  outline: [
    { h: "Getting started", roman: "I.", p: "Upload course materials to generate study notes. Notes can be formatted as outlines, Cornell method, or flashcards.", subs: [] },
  ]
};

const QUIZ_DATA = {
  title: "Practice Quiz",
  meta: [
    { k: "Status", v: "Ready" },
    { k: "Questions", v: "0" },
    { k: "Info", v: "Ask the Assistant to generate a quiz" },
  ],
  questions: []
};

const MINDMAP = {
  id: "root",
  label: "nano-NOTEBOOKLM",
  children: [
    { id: "upload", label: "Upload Sources", children: [
      { id: "pdf", label: "PDF" },
      { id: "pptx", label: "PPTX" },
      { id: "docx", label: "DOCX" },
    ]},
    { id: "study", label: "Study Tools", children: [
      { id: "notes", label: "Notes" },
      { id: "quiz", label: "Quiz" },
      { id: "mindmap", label: "Mind Map" },
    ]},
    { id: "ai", label: "AI Backends", children: [
      { id: "claude", label: "Claude" },
      { id: "gpt", label: "GPT" },
    ]},
  ]
};

Object.assign(window, { SAMPLE_SOURCES, SAMPLE_COLLECTIONS, READER_DOC, NOTES_DATA, QUIZ_DATA, MINDMAP });
