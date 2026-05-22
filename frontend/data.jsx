/* global React, API */
// Data layer — loads real data from API, falls back to mock for demo

const SAMPLE_SOURCES = [
  { id: "s1", type: "pdf", title: "Loading sources...", meta: "connecting to backend", active: true, checked: true, collection: "main" },
];

// Will be dynamically updated when courses load
let SAMPLE_COLLECTIONS = [
  { id: "main", name: "All Courses", count: 0, color: "oklch(0.42 0.08 160)" },
];

// Legacy English-only fallback. Kept so any stray `READER_DOC.chapter` /
// `.title` / `.sub` reference (the header banner at the top of an
// unloaded Reader screen) still renders before the i18n call resolves.
// The expanded operation-guide body lives in `getReaderDoc(t)` below,
// which reader.jsx prefers when in the "no source loaded" intro state.
const READER_DOC = {
  chapter: "nano-NOTEBOOKLM",
  title: "Welcome to nano-NOTEBOOKLM",
  sub: "Upload course materials or select a course to begin exploring.",
  // Minimal defense-in-depth fallback so a script-loading hiccup that
  // strips `getReaderDoc` off `window` still renders one readable
  // paragraph instead of a silent blank pane. See `getReaderDoc(t)` for
  // the real localized operation guide.
  body: [
    { kind: "p", text: "Upload course materials or select a course from the library to begin." },
  ],
};

// Localized welcome / operation guide. Returns the doc shape that
// reader.jsx maps over (`body[].kind` in {"h2", "p"}). The doc covers:
//   1. Getting started (upload + provider setup)
//   2. Core features (Assistant / Reader / Notes / KG / Quiz)
//   3. Advanced (cross-course, embeddings, CLI)
//   4. Troubleshooting
// All strings come from i18n.js so the same shell renders in zh or en.
function getReaderDoc(t) {
  return {
    chapter: t("reader.welcome.chapter"),
    title: t("reader.welcome.title"),
    sub: t("reader.welcome.sub"),
    body: [
      { kind: "p", text: t("reader.welcome.intro") },

      { kind: "h2", num: "1", text: t("reader.welcome.h1") },
      { kind: "h2", num: "1.1", text: t("reader.welcome.s11_h") },
      { kind: "p",  text: t("reader.welcome.s11_p") },
      { kind: "h2", num: "1.2", text: t("reader.welcome.s12_h") },
      { kind: "p",  text: t("reader.welcome.s12_p") },

      { kind: "h2", num: "2", text: t("reader.welcome.h2") },
      { kind: "h2", num: "2.1", text: t("reader.welcome.s21_h") },
      { kind: "p",  text: t("reader.welcome.s21_p") },
      { kind: "h2", num: "2.2", text: t("reader.welcome.s22_h") },
      { kind: "p",  text: t("reader.welcome.s22_p") },
      { kind: "h2", num: "2.3", text: t("reader.welcome.s23_h") },
      { kind: "p",  text: t("reader.welcome.s23_p") },
      { kind: "h2", num: "2.4", text: t("reader.welcome.s24_h") },
      { kind: "p",  text: t("reader.welcome.s24_p") },
      { kind: "h2", num: "2.5", text: t("reader.welcome.s25_h") },
      { kind: "p",  text: t("reader.welcome.s25_p") },

      { kind: "h2", num: "3", text: t("reader.welcome.h3") },
      { kind: "h2", num: "3.1", text: t("reader.welcome.s31_h") },
      { kind: "p",  text: t("reader.welcome.s31_p") },
      { kind: "h2", num: "3.2", text: t("reader.welcome.s32_h") },
      { kind: "p",  text: t("reader.welcome.s32_p") },
      { kind: "h2", num: "3.3", text: t("reader.welcome.s33_h") },
      { kind: "p",  text: t("reader.welcome.s33_p") },

      { kind: "h2", num: "4", text: t("reader.welcome.h4") },
      { kind: "p",  text: t("reader.welcome.s41_p") },
      { kind: "p",  text: t("reader.welcome.s42_p") },
      { kind: "p",  text: t("reader.welcome.s43_p") },
      { kind: "p",  text: t("reader.welcome.s44_p") },
    ],
  };
}

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
      { id: "mindmap", label: "Knowledge Graph" },
    ]},
    { id: "ai", label: "AI Backends", children: [
      { id: "claude", label: "Claude" },
      { id: "gpt", label: "GPT" },
    ]},
  ]
};

Object.assign(window, { SAMPLE_SOURCES, SAMPLE_COLLECTIONS, READER_DOC, NOTES_DATA, QUIZ_DATA, MINDMAP, getReaderDoc });
