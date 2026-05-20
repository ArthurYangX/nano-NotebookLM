"""Contract tests for frontend-only study state helpers.

The app has no build step, so these tests execute plain helper JavaScript with
Node and keep React/Babel out of the test path.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path


def run_node(script: str) -> str:
    proc = subprocess.run(
        ["node", "-e", script],
        cwd=".",
        text=True,
        capture_output=True,
        check=True,
    )
    return proc.stdout


def test_frontend_skill_entries_happy():
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const calls = [];
        const api = {
          analyzeExam: async (course) => { calls.push(['exam', course]); return {patterns:['midterm'], recommendations:['review DP']}; },
          generateReport: async (course) => { calls.push(['report', course]); return {content:'# Report'}; },
          getMastery: async (course) => { calls.push(['mastery', course]); return {weak_areas:[{concept:'gradients', score:0.2}]}; },
        };
        (async () => {
          const entries = h.createSkillEntries(api, 'CS182');
          const out = [];
          for (const e of entries) out.push(await e.run());
          if (calls.length !== 3) throw new Error('expected three fetch calls');
          if (!out[0].text.includes('midterm')) throw new Error('exam fields not rendered');
          if (!out[1].text.includes('Report')) throw new Error('report fields not rendered');
          if (!out[2].text.includes('gradients')) throw new Error('mastery fields not rendered');
          console.log('ok');
        })();
        """
    )
    assert run_node(script).strip() == "ok"


def test_frontend_skill_entries_timeout():
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const api = {
          analyzeExam: async () => { throw new Error('502 upstream'); },
          generateReport: async () => ({content:'ok'}),
          getMastery: async () => ({weak_areas:[]}),
        };
        (async () => {
          const entries = h.createSkillEntries(api, 'CS182');
          const result = await entries[0].run();
          if (result.status !== 'error') throw new Error('expected graceful error state');
          if (!result.text.includes('502 upstream')) throw new Error('missing error detail');
          console.log('ok');
        })();
        """
    )
    assert run_node(script).strip() == "ok"


def test_citation_navigation_happy():
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const sources = [{id:'doc1', title:'ml.pdf'}];
        const result = h.resolveCitationNavigation('[Source: ml.pdf, PDF p.2, chunk c7]', sources);
        if (result.activeId !== 'doc1') throw new Error('wrong source');
        if (result.page !== 2) throw new Error('wrong page');
        if (result.highlightedId !== 'c7') throw new Error('wrong chunk highlight');
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_citation_navigation_invalid():
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const result = h.resolveCitationNavigation('[Source: deleted.pdf, PDF p.99, chunk c7]', [{id:'doc1', title:'ml.pdf'}]);
        if (result.ok) throw new Error('deleted citation should not navigate');
        if (!result.message.includes('deleted.pdf')) throw new Error('missing friendly missing-source message');
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_citation_navigation_bare_integer_page_after_comma():
    """R4-6 regression: LaTeX `\\cite{file:5}` is normalised to `"file, 5"`
    by latex-to-html.normaliseCite — the `p.` prefix is stripped. Before
    this fix, resolveCitationNavigation's pageMatch only accepted `p.N` /
    `page N` / `PDF p.N`, so every R4-6-style cite resolved to page=null
    and every Notes citation jumped to the PDF's first page. The bare-
    integer fallback (after the first comma) restores correct page jumps.
    """
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const sources = [{id:'doc1', title:'lecture.pdf'}];
        // R4-6 normalised form: `file, N` with no `p.` prefix.
        const r1 = h.resolveCitationNavigation('[Source: lecture.pdf, 5]', sources);
        if (!r1.ok) throw new Error('expected ok');
        if (r1.page !== 5) throw new Error('bare-integer page miss: ' + r1.page);
        // Filename containing digits must not leak into page (sourcePart is
        // before the first comma and the bare-int fallback only looks after).
        const sources2 = [{id:'doc1', title:'ch3.pdf'}];
        const r2 = h.resolveCitationNavigation('[Source: ch3.pdf, 7]', sources2);
        if (r2.page !== 7) throw new Error('digit-in-filename poisoned page: ' + r2.page);
        // Slide-prefixed form (PPTX citations) also recognised.
        const r3 = h.resolveCitationNavigation('[Source: deck.pptx, slide 12]', [{id:'d', title:'deck.pptx'}]);
        if (r3.page !== 12) throw new Error('slide N miss: ' + r3.page);
        // Chinese 第 N 页 form.
        const r4 = h.resolveCitationNavigation('[Source: 讲义.pdf, 第 3 页]', [{id:'d', title:'讲义.pdf'}]);
        if (r4.page !== 3) throw new Error('第 N 页 miss: ' + r4.page);
        // Existing `p.N` form still works.
        const r5 = h.resolveCitationNavigation('[Source: lecture.pdf, p.9]', sources);
        if (r5.page !== 9) throw new Error('p.N regression: ' + r5.page);
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_citation_navigation_pptx_filename_not_chunk_id():
    """Regression: pre-fix, the bare-chunk fallback regex `\\b(c[0-9A-Za-z_.:-]+)\\b`
    matched any word starting with `c`, so a citation like
    `[Source: ch3.pptx, p.5]` produced `highlightedId='ch3.pptx'` and the
    Reader hit /api/chunks/ch3.pptx → 404 "chunk not found: ch3.pptx".
    Real chunk_ids always start with `chunk_` (chunker.py:89) — pin that
    contract here so a future loose-regex regression trips the test."""
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const sources = [{id:'doc1', title:'ch3.pptx'}];
        const result = h.resolveCitationNavigation('[Source: ch3.pptx, p.5]', sources);
        if (!result.ok) throw new Error('expected resolution to succeed');
        if (result.activeId !== 'doc1') throw new Error('wrong source id');
        if (result.page !== 5) throw new Error('wrong page');
        // ch3.pptx is a filename, NOT a chunk_id — fallback must emit the
        // synthetic <sourceId>:<page> sentinel that Reader strips.
        if (result.highlightedId !== 'doc1:5') throw new Error(
          'pptx filename leaked into highlightedId: ' + result.highlightedId);
        // And a real chunk_id-shaped citation still parses.
        const real = h.resolveCitationNavigation(
          '[Source: ch3.pptx, p.5, chunk chunk_cs231n_abcd1234_00007]', sources);
        if (real.highlightedId !== 'chunk_cs231n_abcd1234_00007')
          throw new Error('real chunk_id misparsed: ' + real.highlightedId);
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_should_preview_citation():
    """Pin the modal-vs-Reader routing predicate.

    The Notes view dispatches to the in-place PDF modal when canPreview is
    true and falls back to the Reader tab otherwise. Five cases pin the
    contract so a future backend FileType enum change (or accidental field
    drop) trips a test instead of a UX regression.
    """
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const pdfSrc = {id:'a_b', docId:'b', courseId:'a', fileType:'pdf'};
        const pptxSrc = {id:'a_c', docId:'c', courseId:'a', fileType:'pptx'};
        const noDocId = {id:'a_d', docId:null, courseId:'a', fileType:'pdf'};
        const noCourseId = {id:'e', docId:'b', courseId:null, fileType:'pdf'};
        const unknownType = {id:'a_f', docId:'f', courseId:'a', fileType:'epub'};
        const pdf = h.shouldPreviewCitation(pdfSrc);
        if (!pdf.canPreview) throw new Error('pdf source must preview');
        const pptx = h.shouldPreviewCitation(pptxSrc);
        if (pptx.canPreview) throw new Error('pptx without sidecar must fall through');
        if (!pptx.reason.includes('PPTX')) throw new Error('pptx reason missing label');
        // PPTX WITH sidecar (LibreOffice rendered at upload time) must
        // be allowed to preview — /api/source/.../file serves the
        // sidecar with mime=application/pdf so the iframe path works.
        const pptxSidecar = {id:'a_c2', docId:'c2', courseId:'a',
                             fileType:'pptx', viewableAsPdf:true};
        const pptxOk = h.shouldPreviewCitation(pptxSidecar);
        if (!pptxOk.canPreview) throw new Error(
          'pptx with viewableAsPdf=true must preview, got: ' + JSON.stringify(pptxOk));
        const missing = h.shouldPreviewCitation(noDocId);
        if (missing.canPreview) throw new Error('missing docId must fall through');
        const missingCourse = h.shouldPreviewCitation(noCourseId);
        if (missingCourse.canPreview) throw new Error('missing courseId must fall through');
        const unknown = h.shouldPreviewCitation(unknownType);
        if (unknown.canPreview) throw new Error('unknown fileType must fall through');
        const undef = h.shouldPreviewCitation(undefined);
        if (undef.canPreview) throw new Error('undefined source must fall through');
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_mindmap_layout_happy():
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const kg = {nodes: [], edges: []};
        for (let i = 0; i < 30; i++) kg.nodes.push({id:'n'+i, name:'Node '+i, depth:i === 0 ? 0 : 1, weight:i + 1, source_chunks:[{source_file:'ml.pdf', page:1, chunk_id:'c'+i}]});
        for (let i = 1; i < 30; i++) kg.edges.push({source:'n0', target:'n'+i, relation:'depends-on'});
        const layout = h.prepareMindmap(kg, {layout:'radial'});
        if (layout.nodes.length !== 30) throw new Error('missing nodes');
        if (!(layout.nodes[29].style.fontSize > layout.nodes[1].style.fontSize)) throw new Error('weight should affect font size');
        if (!h.getMindmapNodeDetail(layout, 'n12').source_chunks[0].chunk_id) throw new Error('missing detail sources');
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_mindmap_layout_empty():
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const layout = h.prepareMindmap({nodes: [], edges: []});
        if (layout.empty !== true) throw new Error('empty KG should have placeholder');
        if (!layout.placeholder) throw new Error('missing empty placeholder');
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_notes_edit_export_happy():
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const store = h.createMemoryStorage();
        h.saveNoteDraft(store, 'CS182', '# Edited');
        const draft = h.loadNoteDraft(store, 'CS182');
        const exp = h.buildMarkdownExport('CS182', draft);
        if (draft !== '# Edited') throw new Error('draft not persisted');
        if (exp.filename !== 'CS182-notes.md') throw new Error('bad filename');
        if (!exp.content.includes('# Edited')) throw new Error('bad content');
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_notes_edit_large():
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const store = h.createMemoryStorage();
        const big = 'x'.repeat(120 * 1024);
        h.saveNoteDraft(store, 'CS182', big);
        h.saveNoteDraft(store, 'CS285', 'other');
        if (h.loadNoteDraft(store, 'CS182').length !== big.length) throw new Error('large draft lost');
        if (h.loadNoteDraft(store, 'CS285') !== 'other') throw new Error('course switch lost draft');
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


# review-swarm fix-all v1 #16: pin the new LaTeX-refactor frontend helpers.


def test_build_latex_export_shape():
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const exp = h.buildLatexExport('CS182', '\\\\section{Hi}');
        if (exp.filename !== 'CS182-notes.tex') throw new Error('bad filename: ' + exp.filename);
        if (!exp.mime.includes('text/x-tex')) throw new Error('bad mime: ' + exp.mime);
        if (!exp.content.includes('\\\\section{Hi}')) throw new Error('content missing');
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_build_print_html_self_contained():
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const html = h.buildPrintHtml('CS182', '<h2>Hi</h2><div class="thm-box">x</div>');
        if (!html.includes('katex@0.16.11')) throw new Error('katex assets missing');
        if (!html.includes('<h2>Hi</h2>')) throw new Error('body missing');
        if (!html.includes('renderMathInElement')) throw new Error('auto-render bootstrap missing');
        if (!html.includes('thm-box')) throw new Error('theorem-box css missing');
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_legacy_markdown_note_draft_discarded_on_first_load():
    script = textwrap.dedent(
        """
        // Silence the one-time discard log so the test output is just 'ok'.
        console.info = function () {};
        const h = require('./frontend/study-state.js');
        const s = h.createMemoryStorage();
        // Seed the legacy key.
        s.setItem('nano-nlm:v1:CS182:notes:draft', '# old markdown note');
        // First load should discard, return empty, set the per-course flag.
        const loaded = h.loadNoteDraft(s, 'CS182');
        if (loaded !== '') throw new Error('legacy draft survived: ' + loaded);
        if (s.getItem('nano-nlm:v1:CS182:notes:draft') !== null) {
          throw new Error('legacy key not cleared');
        }
        if (s.getItem('nano-nlm:v1:CS182:notes-migration-logged') !== '1') {
          throw new Error('migration flag not set');
        }
        // Saving a new LaTeX draft writes to the latex key, leaving the legacy untouched.
        h.saveNoteDraft(s, 'CS182', '\\\\section{New}');
        if (s.getItem('nano-nlm:v1:CS182:notes-latex:draft') !== '\\\\section{New}') {
          throw new Error('new key not written');
        }
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_quiz_persistence_happy():
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const store = h.createMemoryStorage();
        const quiz = [{question:'q1', correct:'A'}, {question:'q2', answer:'x'}];
        h.saveQuizAnswers(store, 'CS182', quiz, {'0':'A', '1':'y'});
        const loaded = h.loadQuizAnswers(store, 'CS182', quiz);
        const wrong = h.filterWrongQuestions(quiz, loaded.answers);
        if (loaded.stale) throw new Error('answers should not be stale');
        if (wrong.length !== 1 || wrong[0].question !== 'q2') throw new Error('wrong filter failed');
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_correct_letter_helper_handles_all_answer_formats():
    """fix-all v1 H5 + M4: pin the shared helper so the regex regression
    (bare-letter answers like 'A' being missed) can't come back. Five cases
    cover legacy `correct:'A'`, LLM 'B. text', 'C) text', lowercase, bare
    letter, and missing fields."""
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const tests = [
          [{correct: 'A'}, 'A'],
          [{answer: 'B. printf is part of the standard I/O library'}, 'B'],
          [{answer: 'C) explanation'}, 'C'],
          [{answer: 'd. lowercase'}, 'D'],
          [{answer: 'A'}, 'A'],   // <-- this is the bare-letter regression
          [{answer: 'just an essay answer with leading lowercase'}, ''],
          [{}, ''],
          [null, ''],
        ];
        for (const [q, want] of tests) {
          const got = h.correctLetter(q);
          if (got !== want) {
            throw new Error('correctLetter(' + JSON.stringify(q) + ') = ' + JSON.stringify(got) + ' want ' + JSON.stringify(want));
          }
        }
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_wrong_only_filter_uses_letter_for_multi_choice():
    """fix-all v1 H5: the LLM-emitted answer field is 'B. full text' but
    the user picks store as a bare letter 'B'. Pre-fix, filterWrongQuestions
    compared 'B' !== 'B. full text' → every answered multi-choice question
    fell out as wrong. Verify the letter-based comparison."""
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const quiz = [
          {question: 'q1', type: 'multiple_choice', answer: 'B. printf is the right call', options: ['A','B','C','D']},
          {question: 'q2', type: 'multiple_choice', answer: 'A. the wrong one', options: ['A','B','C','D']},
          {question: 'q3', type: 'short_answer', answer: 'backpropagation'},
        ];
        // User answered q1 correctly ('B'), q2 wrong ('B' but answer is A),
        // q3 correctly with substring match.
        const answers = {'0': 'B', '1': 'B', '2': 'backpropagation'};
        const wrong = h.filterWrongQuestions(quiz, answers);
        if (wrong.length !== 1) throw new Error('expected 1 wrong, got ' + wrong.length);
        if (wrong[0].question !== 'q2') throw new Error('expected q2 wrong, got ' + wrong[0].question);
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_find_courses_with_cache_resurfaces_preset_work():
    """R5-2 fix-all v2 #1: in default mode (?show_preset NOT set) the
    backend hides preset courses, so localStorage cache the user built up
    in ?show_preset=1 mode becomes invisible — they can't even navigate to
    the course in the dropdown. `findCoursesWithCache` scans storage for
    content-cache keys (notes/highlights/mindmap/quiz/latex-draft) so the
    UI can resurface those preset courses with a "(cached)" label."""
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const s = h.createMemoryStorage();
        // Simulate work the user did in ?show_preset=1 mode on preset courses
        s.setItem('nano-nlm:v1:CS231N:notes', '\\\\section{Backprop}');
        s.setItem('nano-nlm:v1:CS231N:notes:highlights', '[]');
        s.setItem('nano-nlm:v1:15-213:mindmap', '{"nodes":[]}');
        s.setItem('nano-nlm:v1:uploaded-foo:quiz', '[]');
        // Config-only keys MUST NOT count as "real work" — otherwise just
        // visiting a preset course resurfaces it forever.
        s.setItem('nano-nlm:v1:noise-only:notes-toc-collapsed', '[]');
        s.setItem('nano-nlm:v1:noise-only:notes-scroll-y', '100');
        // Global keys (no course segment) MUST be skipped.
        s.setItem('nano-nlm:v1:hidden-courses', '[]');
        s.setItem('nano-nlm:v1:backend', 'codex');
        // Pseudo "_all_" sentinel from null-course paths MUST be skipped.
        s.setItem('nano-nlm:v1:_all_:notes', 'whatever');

        const found = h.findCoursesWithCache(s);
        const want = ['15-213', 'CS231N', 'uploaded-foo'];
        if (JSON.stringify(found) !== JSON.stringify(want)) {
          throw new Error('expected ' + JSON.stringify(want) + ' got ' + JSON.stringify(found));
        }
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_notes_toc_hidden_pref_persists_globally():
    """R5-2 fix-all v4 #2: the user's "I find the TOC too noisy" pref
    must survive a reload. Key is global (no course segment) so it
    applies everywhere, mirroring backend / kg-legend-hidden."""
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const s = h.createMemoryStorage();
        // Default: TOC visible (loadNotesTocHidden returns false).
        if (h.loadNotesTocHidden(s) !== false) throw new Error('default should be visible');
        h.saveNotesTocHidden(s, true);
        if (h.loadNotesTocHidden(s) !== true) throw new Error('after save: hidden');
        h.saveNotesTocHidden(s, false);
        if (h.loadNotesTocHidden(s) !== false) throw new Error('after toggle: visible');
        // Stored under the exact global key.
        if (s.getItem('nano-nlm:v1:notes-toc-hidden') !== '0') {
          throw new Error('expected global key, got: ' + JSON.stringify(s.dump()));
        }
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_notes_toolbar_collapsed_pref_persists_globally():
    """Notes toolbar collapse state persists across reloads under the
    same global-pref convention as :backend / :kg-legend-hidden /
    :notes-toc-hidden. Default is false (expanded) so first-time users
    still see the action buttons."""
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const s = h.createMemoryStorage();
        // Default: toolbar expanded (loadNotesToolbarCollapsed returns false).
        if (h.loadNotesToolbarCollapsed(s) !== false) throw new Error('default should be expanded');
        h.saveNotesToolbarCollapsed(s, true);
        if (h.loadNotesToolbarCollapsed(s) !== true) throw new Error('after save: collapsed');
        h.saveNotesToolbarCollapsed(s, false);
        if (h.loadNotesToolbarCollapsed(s) !== false) throw new Error('after toggle: expanded');
        // Stored under the exact global key.
        if (s.getItem('nano-nlm:v1:notes-toolbar-collapsed') !== '0') {
          throw new Error('expected global key, got: ' + JSON.stringify(s.dump()));
        }
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_pdf_outline_hidden_pref_defaults_to_hidden():
    """R5-2 fix-all v4 #2: PDFium's bookmark/thumbnail pane is rarely
    useful for short course slides, so we default HIDDEN — user opts in
    via the floating toggle in Reader. Backward-compat: absent key OR
    explicit "1" both hide; only "0" reveals."""
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const s = h.createMemoryStorage();
        // Absent key → hidden.
        if (h.loadPdfOutlineHidden(s) !== true) throw new Error('default should be hidden');
        h.savePdfOutlineHidden(s, false);
        if (h.loadPdfOutlineHidden(s) !== false) throw new Error('after reveal save: visible');
        h.savePdfOutlineHidden(s, true);
        if (h.loadPdfOutlineHidden(s) !== true) throw new Error('after hide save: hidden');
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_source_file_url_appends_navpanes_zero_when_outline_hidden():
    """Pin the fragment-shape contract so the Reader's PDF iframe always
    gets `#navpanes=0` when the user collapses the outline, but never
    smuggles the param in when they want the panes visible."""
    script = textwrap.dedent(
        """
        // We test the API helper directly. Need to stub window since api.js
        // is browser-flavoured; load via vm with a minimal globalThis fill.
        const fs = require('fs');
        const src = fs.readFileSync('./frontend/api.js', 'utf-8');
        // Extract the sourceFileUrl function via a regex (simpler than vm).
        const m = src.match(/sourceFileUrl\\([\\s\\S]+?\\n  \\},/);
        if (!m) throw new Error('sourceFileUrl not found');
        const fn = new Function('encodeURIComponent', 'Number',
          `let frags=[]; const o = { sourceFileUrl${m[0].slice('sourceFileUrl'.length).replace(/,$/, '')} }; return o.sourceFileUrl;`);
        const sourceFileUrl = fn(encodeURIComponent, Number);

        // Neither flag → bare URL.
        const a = sourceFileUrl('c', 'd');
        if (a.includes('#')) throw new Error('bare URL must have no fragment: ' + a);

        // Only page → page=N alone.
        const b = sourceFileUrl('c', 'd', { page: 5 });
        if (!b.endsWith('#page=5')) throw new Error('page-only frag wrong: ' + b);

        // Only hideOutline → navpanes=0 alone.
        const c = sourceFileUrl('c', 'd', { hideOutline: true });
        if (!c.endsWith('#navpanes=0')) throw new Error('outline-only frag wrong: ' + c);

        // Both → page=N&navpanes=0 in order.
        const d = sourceFileUrl('c', 'd', { page: 5, hideOutline: true });
        if (!d.endsWith('#page=5&navpanes=0')) throw new Error('both frags wrong: ' + d);

        // hideOutline=false → no navpanes param (don't pin the user's
        // existing PDFium UI preference).
        const e = sourceFileUrl('c', 'd', { page: 5, hideOutline: false });
        if (e.includes('navpanes')) throw new Error('false hideOutline must not append: ' + e);

        // R5-2 fix-all v8: navEpoch must land in the QUERY (`?_nav=N`)
        // not the fragment, so each navigation forces a fresh browser
        // fetch and PDFium re-init. Pin the contract here so a future
        // refactor that moves it into the hash breaks loudly.
        const f = sourceFileUrl('c', 'd', { page: 5, navEpoch: 7 });
        if (!f.includes('?_nav=7')) throw new Error('navEpoch must land in query: ' + f);
        if (!f.endsWith('#page=5')) throw new Error('hash should still carry page: ' + f);

        // navEpoch=0 → omit (treat as "no nav has happened yet").
        const g = sourceFileUrl('c', 'd', { page: 5, navEpoch: 0 });
        if (g.includes('_nav')) throw new Error('navEpoch=0 must NOT inject _nav: ' + g);

        // navEpoch + hideOutline → query + dual frags both present.
        const h2 = sourceFileUrl('c', 'd', { page: 9, hideOutline: true, navEpoch: 3 });
        if (!h2.includes('?_nav=3')) throw new Error('combined: missing _nav: ' + h2);
        if (!h2.endsWith('#page=9&navpanes=0')) throw new Error('combined: hash wrong: ' + h2);

        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_find_courses_with_cache_handles_missing_storage():
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        // No `.length` / no `.key` → empty result, no throw.
        if (JSON.stringify(h.findCoursesWithCache(null)) !== '[]') throw new Error('null storage');
        if (JSON.stringify(h.findCoursesWithCache({})) !== '[]') throw new Error('bare {} storage');
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_quiz_persistence_invalid():
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const store = h.createMemoryStorage();
        h.saveQuizAnswers(store, 'CS182', [{question:'old'}], {'0':'A'});
        const loaded = h.loadQuizAnswers(store, 'CS182', [{question:'new'}]);
        if (!loaded.stale) throw new Error('changed quiz should mark answers stale');
        if (!loaded.message.includes('stale')) throw new Error('missing stale prompt');
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_mastery_targeted_quiz_happy():
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const calls = [];
        const api = { generateQuiz: async (course, topic) => { calls.push([course, topic]); return {quiz:[{question:'q'}]}; } };
        (async () => {
          const result = await h.generateWeakAreaQuiz(api, 'CS182', {concept:'gradients'});
          if (calls[0][1] !== 'gradients') throw new Error('topic not passed');
          if (result.quiz.length !== 1) throw new Error('quiz result not returned');
          console.log('ok');
        })();
        """
    )
    assert run_node(script).strip() == "ok"


def test_mastery_empty():
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const state = h.formatMasteryState({mastery:{a:{score:0.8}}, weak_areas:[]});
        if (!state.empty) throw new Error('all scores >=0.5 should be empty state');
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_retry_generation_happy():
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        let state = h.createGenerationState();
        state = h.recordPartialGeneration(state, 'draft');
        state = h.recordGenerationFailure(state, new Error('bad'), 1);
        state = h.retryGeneration(state);
        if (state.partial !== 'draft') throw new Error('partial should survive retry');
        if (state.status !== 'retrying') throw new Error('retry not started');
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_recordGenerationFailure_translates_known_stable_codes():
    """fix-all v4 follow-up: stable backend codes (`stream_failed`,
    `upstream_error`, ...) must be translated into a user-facing string —
    we don't want users seeing the raw token in `errorDetail`. Unknown
    codes pass through verbatim so legacy err.message strings still work."""
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        let s1 = h.createGenerationState();
        s1 = h.recordGenerationFailure(s1, new Error('stream_failed'), 1);
        if (!s1.errorDetail.includes('生成失败') && !s1.errorDetail.includes('Generation failed')) {
          throw new Error('stream_failed not translated: ' + s1.errorDetail);
        }
        if (s1.errorDetail === 'stream_failed') {
          throw new Error('raw stable code leaked into errorDetail');
        }
        if (s1.errorCode !== 'stream_failed') {
          throw new Error('errorCode missing or wrong: ' + s1.errorCode);
        }
        let s2 = h.createGenerationState();
        s2 = h.recordGenerationFailure(s2, new Error('totally_unknown_code'), 1);
        if (!s2.errorDetail.includes('totally_unknown_code')) {
          throw new Error('unknown code should pass through, got ' + s2.errorDetail);
        }
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_retry_generation_timeout():
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        let state = h.createGenerationState();
        state = h.recordGenerationFailure(state, new Error('one'), 1);
        state = h.recordGenerationFailure(state, new Error('two'), 2);
        state = h.recordGenerationFailure(state, new Error('three'), 3);
        if (state.status !== 'failed') throw new Error('should stop after 3 failures');
        if (!state.errorDetail.includes('three')) throw new Error('missing final error detail');
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_checked_source_files_strips_prefix_happy():
    """All Courses mode prepends '[course] ' to title for display, but the
    backend qa_skill compares against raw source_file. The helper must return
    the raw filename so the filter actually matches."""
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const sources = [
          { id: 's1', title: '[计算机组成原理] 第五章.pdf', sourceFile: '第五章.pdf', checked: true },
          { id: 's2', title: '第一章.pdf', sourceFile: '第一章.pdf', checked: true },
          { id: 's3', title: '[CS182] hidden.pdf', sourceFile: 'hidden.pdf', checked: false },
        ];
        const out = h.getCheckedSourceFiles(sources);
        if (out.length !== 2) throw new Error('expected 2 checked files, got ' + out.length);
        if (out[0] !== '第五章.pdf') throw new Error('All Courses prefix not stripped: ' + out[0]);
        if (out[1] !== '第一章.pdf') throw new Error('single course filename mangled: ' + out[1]);
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_notes_highlights_crud_happy():
    """Mini: create / update / remove + per-course isolation."""
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const store = h.createMemoryStorage();
        let list = h.addHighlight(store, 'CS182', { text: 'gradient descent', before: 'minimize loss via ', after: ' until convergence', color: 'yellow', note: 'core idea' });
        if (list.length !== 1) throw new Error('first add failed');
        const hid = list[0].id;
        if (!hid || !hid.startsWith('h_')) throw new Error('id format wrong: ' + hid);
        list = h.addHighlight(store, 'CS182', { text: 'backprop', color: 'green' });
        list = h.addHighlight(store, 'CS285', { text: 'policy gradient', color: 'pink' });
        if (h.loadHighlights(store, 'CS182').length !== 2) throw new Error('CS182 should have 2');
        if (h.loadHighlights(store, 'CS285').length !== 1) throw new Error('CS285 should have 1');

        list = h.updateHighlight(store, 'CS182', hid, { note: 'updated note', color: 'pink' });
        const updated = list.find(x => x.id === hid);
        if (updated.note !== 'updated note') throw new Error('note not updated');
        if (updated.color !== 'pink') throw new Error('color not updated');

        list = h.removeHighlight(store, 'CS182', hid);
        if (list.length !== 1) throw new Error('remove failed');
        if (list.find(x => x.id === hid)) throw new Error('removed item still present');
        if (h.loadHighlights(store, 'CS285').length !== 1) throw new Error('cross-course leak');
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_notes_highlights_locate_with_context():
    """Mini: locateHighlight uses before/after to disambiguate the same text."""
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const md = '# A\\nThe loss is small. Then the loss is large. End.';
        const idx1 = h.locateHighlight(md, { text: 'loss', before: 'The ', after: ' is small' });
        const idx2 = h.locateHighlight(md, { text: 'loss', before: 'Then the ', after: ' is large' });
        if (idx1 < 0 || idx2 < 0) throw new Error('both should locate');
        if (idx1 === idx2) throw new Error('context did not disambiguate');
        if (md.substr(idx1, 4) !== 'loss') throw new Error('idx1 not aligned to "loss"');
        if (md.substr(idx2, 4) !== 'loss') throw new Error('idx2 not aligned to "loss"');
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_notes_highlights_prune_stale():
    """Corner: data missing — when raw markdown no longer contains highlight text,
    pruneStaleHighlights drops it (returns it in `removed`)."""
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const store = h.createMemoryStorage();
        h.addHighlight(store, 'CS182', { text: 'kept phrase', before: '', after: '' });
        h.addHighlight(store, 'CS182', { text: 'phrase that was deleted', before: '', after: '' });
        const editedContent = '# A\\nThis content has the kept phrase but nothing else.';
        const result = h.pruneStaleHighlights(store, 'CS182', editedContent);
        if (result.kept.length !== 1) throw new Error('expected 1 kept, got ' + result.kept.length);
        if (result.removed.length !== 1) throw new Error('expected 1 removed, got ' + result.removed.length);
        if (result.kept[0].text !== 'kept phrase') throw new Error('wrong kept item');
        // Persisted state should reflect the prune.
        if (h.loadHighlights(store, 'CS182').length !== 1) throw new Error('storage not pruned');
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_notes_highlights_reject_empty_selection():
    """Corner: invalid format — empty / whitespace-only selection is rejected."""
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const store = h.createMemoryStorage();
        h.addHighlight(store, 'CS182', { text: '   ', color: 'yellow' });
        h.addHighlight(store, 'CS182', { text: '', color: 'green' });
        if (h.loadHighlights(store, 'CS182').length !== 0) throw new Error('empty selection should be rejected');
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_notes_highlights_survives_markdown_controls():
    """Corner: boundary — selection text spans across markdown control chars
    (**bold**, [Source: ...]) and locateHighlight still finds it because we
    anchor by raw markdown text, not by rendered html."""
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const md = '## Section\\nGradient descent is a **first-order** method using [Source: notes.pdf, p.3].';
        const hl = { text: 'first-order** method using [Source: notes.pdf', before: 'a **', after: ', p.3]' };
        const idx = h.locateHighlight(md, hl);
        if (idx < 0) throw new Error('cross-control highlight should still locate');
        if (md.substr(idx, hl.text.length) !== hl.text) throw new Error('idx misaligned');
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_notes_toc_extracts_three_levels():
    """Mini: extractHeadingTOC pulls H1/H2/H3 with stable slug ids."""
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const md = '# Course Overview\\nintro text\\n## Module 1\\nbody\\n### Topic A\\nmore\\n## Module 2\\n#### Skipped (h4)';
        const toc = h.extractHeadingTOC(md);
        if (toc.length !== 4) throw new Error('expected 4 (h1+h2+h3+h2), got ' + toc.length);
        if (toc[0].level !== 1 || toc[0].text !== 'Course Overview') throw new Error('wrong h1');
        if (toc[2].level !== 3 || toc[2].text !== 'Topic A') throw new Error('wrong h3');
        if (toc[0].id !== 'course-overview') throw new Error('wrong slug: ' + toc[0].id);
        if (toc[2].id !== 'topic-a') throw new Error('wrong slug: ' + toc[2].id);
        // Same heading text twice → suffixed ids
        const md2 = '# Intro\\n## Intro\\n## Intro';
        const toc2 = h.extractHeadingTOC(md2);
        if (toc2[0].id === toc2[1].id) throw new Error('duplicate slugs not disambiguated');
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_notes_toc_dedupe_three_or_more_duplicates():
    """Corner: 3+ identical headings get distinct ids — fixed dedupe over the
    earlier counter-based logic that double-bumped seen[id] and produced
    colliding ids on the third occurrence."""
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const md = '# Intro\\n## Intro\\n## Intro\\n## Intro\\n## Intro';
        const toc = h.extractHeadingTOC(md);
        const ids = toc.map(t => t.id);
        const unique = new Set(ids);
        if (unique.size !== ids.length) throw new Error('duplicate slugs survived: ' + ids.join(','));
        if (ids[0] !== 'intro' || ids[1] !== 'intro-1' || ids[2] !== 'intro-2' || ids[3] !== 'intro-3' || ids[4] !== 'intro-4') {
          throw new Error('unexpected slug sequence: ' + ids.join(','));
        }
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_notes_toc_empty_and_no_headings():
    """Corner: data variety — empty markdown / no-heading markdown returns []."""
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        if (h.extractHeadingTOC('').length !== 0) throw new Error('empty should be []');
        if (h.extractHeadingTOC(null).length !== 0) throw new Error('null should be []');
        if (h.extractHeadingTOC('plain text\\nno hashes').length !== 0) throw new Error('no-heading should be []');
        if (h.slugifyHeadingsList('').length !== 0) throw new Error('shared helper empty should be []');
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_notes_highlights_recover_from_corrupt_storage():
    """Corner: data missing — corrupt JSON in localStorage doesn't crash
    loadHighlights, and a subsequent addHighlight still works."""
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const store = h.createMemoryStorage({'nano-nlm:v1:CS182:notes:highlights': '{not-valid-json'});
        const list1 = h.loadHighlights(store, 'CS182');
        if (!Array.isArray(list1) || list1.length !== 0) throw new Error('corrupt storage should yield []');
        const list2 = h.addHighlight(store, 'CS182', { text: 'after recovery', color: 'green' });
        if (list2.length !== 1) throw new Error('add after corrupt failed');
        if (h.loadHighlights(store, 'CS182').length !== 1) throw new Error('post-recovery load failed');
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_notes_highlights_drops_unknown_color_on_load():
    """Corner: illegal format on disk — tampered localStorage with unknown
    color is filtered out by loadHighlights so it never reaches className."""
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const stored = JSON.stringify([
          { id: 'h1', text: 'kept', before: '', after: '', color: 'yellow', note: '' },
          { id: 'h2', text: 'dropped', before: '', after: '', color: 'rainbow; expression(x)', note: '' },
        ]);
        const store = h.createMemoryStorage({'nano-nlm:v1:CS182:notes:highlights': stored});
        const list = h.loadHighlights(store, 'CS182');
        if (list.length !== 1) throw new Error('expected 1 kept, got ' + list.length);
        if (list[0].id !== 'h1') throw new Error('wrong survivor');
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_notes_toc_slug_parity_with_markdownToHtml():
    """Mini: upstream consistency — markdownToHtml MUST emit heading id
    attributes that exactly match the slug ids extractHeadingTOC produces.
    TOC clicks rely on this contract; if either function dedupes
    differently, jumpToHeading silently no-ops or jumps to the wrong section."""
    # markdownToHtml lives in app.jsx but uses StudyState.slugifyHeadingsList,
    # so we verify by running the helper on the same source.
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const md = '# Intro\\n## Background\\n### Setup\\n## Background\\n# Conclusions\\n## Background';
        const tocList = h.slugifyHeadingsList(md);
        const tocOnly = h.extractHeadingTOC(md);
        if (tocList.length !== tocOnly.length) throw new Error('length mismatch');
        for (let i = 0; i < tocList.length; i++) {
          if (tocList[i].id !== tocOnly[i].id) throw new Error('slug mismatch at ' + i + ': ' + tocList[i].id + ' vs ' + tocOnly[i].id);
        }
        const ids = tocList.map(t => t.id);
        const unique = new Set(ids);
        if (unique.size !== ids.length) throw new Error('duplicate ids: ' + ids.join(','));
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_notes_highlights_save_survives_quota_exception():
    """Corner: upstream failure — saveHighlights swallows quota exceptions
    so the in-memory list still propagates back to the React state and the
    UI doesn't crash on Safari private mode."""
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const failingStore = {
          getItem: () => null,
          setItem: () => { const e = new Error('QuotaExceededError'); e.name = 'QuotaExceededError'; throw e; },
          removeItem: () => {},
        };
        // addHighlight calls saveHighlights internally — should NOT throw.
        let list;
        try {
          list = h.addHighlight(failingStore, 'CS182', { text: 'phrase', color: 'yellow' });
        } catch (err) {
          throw new Error('addHighlight should not throw on quota: ' + err.message);
        }
        // In-memory list still grows for current session even if persistence failed.
        if (list.length !== 1 || list[0].text !== 'phrase') throw new Error('in-memory state lost');
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_notes_toc_handles_cjk_headings():
    """Corner: data variety — CJK headings keep their characters in the slug."""
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const md = '# 第一章 引论\\n## 1.1 概述\\n### 损失函数';
        const toc = h.extractHeadingTOC(md);
        if (toc.length !== 3) throw new Error('expected 3 entries');
        if (!toc[0].id.includes('第一章')) throw new Error('CJK stripped from slug: ' + toc[0].id);
        if (!toc[2].id.includes('损失函数')) throw new Error('CJK stripped from slug: ' + toc[2].id);
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_checked_source_files_legacy_title_fallback():
    """Corner: source objects without explicit sourceFile (legacy / new uploads)
    must still produce a usable filter value — strip a leading bracketed prefix
    if present so the qa filter can still hit on All Courses titles."""
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const sources = [
          { id: 's1', title: '[机器人导论] sensors.pdf', checked: true },
          { id: 's2', title: 'plain.pdf', checked: true },
          { id: 's3', title: '[edge] [nested] weird.pdf', checked: true },
          { id: 's4', title: '[unchecked] x.pdf', checked: false },
        ];
        const out = h.getCheckedSourceFiles(sources);
        if (out.length !== 3) throw new Error('expected 3 checked files, got ' + out.length);
        if (out[0] !== 'sensors.pdf') throw new Error('legacy bracket strip failed: ' + out[0]);
        if (out[1] !== 'plain.pdf') throw new Error('plain title mangled: ' + out[1]);
        // Only strip ONE leading [...] prefix so legitimate filenames containing
        // brackets do not get over-eaten.
        if (out[2] !== '[nested] weird.pdf') throw new Error('over-stripped nested brackets: ' + out[2]);
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_toc_tree_extraction_file_wrappers():
    """extractTOC builds a hierarchical tree:
      - \\section{<filename>}     → L1 file root
      - \\section{Topic} (no ext) → L2 nested under prev L1
      - \\subsection{Sub}         → L2 nested under prev L1
      - \\subsubsection{Detail}   → L3 nested under prev L2
    The filename heuristic catches `.pdf` titles even without an explicit
    fileNames whitelist.
    """
    script = textwrap.dedent(
        r"""
        const tex = require('./frontend/latex-to-html.js');
        const src = '\\section{lecture3.pdf}\n' +
                    '\\subsection{Backprop}\n' +
                    '\\subsubsection{Chain rule}\n' +
                    '\\section{Other thoughts}\n' +
                    '\\section{lecture4.pdf}\n' +
                    '\\subsection{Conv layer}\n';
        const tree = tex.extractTOC(src);
        if (tree.length !== 2) throw new Error('expected 2 L1 file roots, got: ' + tree.length);
        if (tree[0].text !== 'lecture3.pdf') throw new Error('L1[0] mislabelled: ' + tree[0].text);
        if (tree[1].text !== 'lecture4.pdf') throw new Error('L1[1] mislabelled: ' + tree[1].text);
        // L3 should sit under L2 (Backprop), L2 should sit under L1 (lecture3.pdf).
        const l3 = tree[0].children[0].children[0];
        if (!l3 || l3.text !== 'Chain rule') throw new Error('L3 chain failed: ' + JSON.stringify(tree[0]));
        // "Other thoughts" is a non-filename \section → must nest under
        // the most recent L1 (lecture3.pdf), not start a new L1.
        const otherIdx = tree[0].children.findIndex(c => c.text === 'Other thoughts');
        if (otherIdx < 0) throw new Error('non-file section did not nest: ' + JSON.stringify(tree[0]));
        // ids should be unique and slug-like.
        const ids = new Set();
        function walk(n) { ids.add(n.id); (n.children||[]).forEach(walk); }
        tree.forEach(walk);
        if (ids.size !== 6) throw new Error('expected 6 unique ids, got: ' + ids.size);
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_toc_tree_zero_child_file_wrapper():
    """A `\\section{lecture7.pdf}` with no `\\subsection` underneath
    produces an L1 file root with `children: []`. This is a real shape
    the LLM emits for slide decks too short to need subsections. The
    NotesTOC renderer handles 0-child L1s by replacing the triangle
    with a spacer (no collapse affordance, but the row stays jump-
    clickable). Pin the shape to catch regressions in the extractor's
    file-wrapper grouping."""
    script = textwrap.dedent(
        r"""
        const tex = require('./frontend/latex-to-html.js');
        const src = '\\section{lecture7.pdf}\n' +
                    '\\section{lecture8.pdf}\n' +
                    '\\subsection{Intro}\n';
        const tree = tex.extractTOC(src);
        if (tree.length !== 2) throw new Error('expected 2 L1 roots, got: ' + tree.length);
        if (tree[0].text !== 'lecture7.pdf') throw new Error('L1[0] text: ' + tree[0].text);
        if (!Array.isArray(tree[0].children) || tree[0].children.length !== 0) {
          throw new Error('L1[0] should have empty children, got: ' + JSON.stringify(tree[0].children));
        }
        if (tree[1].text !== 'lecture8.pdf') throw new Error('L1[1] text');
        if (tree[1].children.length !== 1 || tree[1].children[0].text !== 'Intro') {
          throw new Error('L1[1] children malformed: ' + JSON.stringify(tree[1].children));
        }
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_toc_tree_filename_whitelist_overrides_heuristic():
    """When fileNames is supplied, a title matching it becomes L1 even
    without a recognised extension (handles user uploads named e.g.
    `Lecture 03 - Intro` with no suffix)."""
    script = textwrap.dedent(
        r"""
        const tex = require('./frontend/latex-to-html.js');
        const src = '\\section{Lecture 03 - Intro}\n' +
                    '\\subsection{Background}\n' +
                    '\\section{Random topic}\n';
        const tree = tex.extractTOC(src, { fileNames: ['Lecture 03 - Intro'] });
        if (tree.length !== 1) throw new Error('expected 1 L1 file root: ' + tree.length);
        if (tree[0].text !== 'Lecture 03 - Intro') throw new Error('wrong L1');
        // Both children nest under it (one \subsection + one non-file \section).
        if (tree[0].children.length !== 2) throw new Error('children: ' + tree[0].children.length);
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_toc_collapsed_roundtrip():
    """loadTocCollapsed / saveTocCollapsed / setTocCollapsed round-trip
    via localStorage, dedup + sort + drop non-strings."""
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const s = h.createMemoryStorage();

        if (h.loadTocCollapsed(s, 'CS182').length !== 0) throw new Error('default not empty');

        let next = h.setTocCollapsed(s, 'CS182', 'lecture3-pdf', true);
        if (next.length !== 1) throw new Error('collapse add failed');

        next = h.setTocCollapsed(s, 'CS182', 'lecture4-pdf', true);
        if (JSON.stringify(next) !== '["lecture3-pdf","lecture4-pdf"]')
          throw new Error('sort failed: ' + JSON.stringify(next));

        next = h.setTocCollapsed(s, 'CS182', 'lecture3-pdf', false);
        if (JSON.stringify(next) !== '["lecture4-pdf"]') throw new Error('uncollapse failed');

        // Per-course isolation.
        h.setTocCollapsed(s, 'CS285', 'foo', true);
        if (h.loadTocCollapsed(s, 'CS182').length !== 1) throw new Error('CS182 leaked');

        // Bad input filtered out.
        h.saveTocCollapsed(s, 'CS182', ['ok', 99, null, 'dup', 'dup']);
        const cleaned = h.loadTocCollapsed(s, 'CS182');
        if (JSON.stringify(cleaned) !== '["dup","ok"]') throw new Error('non-string filter: ' + JSON.stringify(cleaned));

        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_adapt_flat_toc_to_tree():
    """adaptFlatTocToTree wraps the markdown-fallback flat list into a
    tree so the new tree-rendering NotesTOC can consume both shapes."""
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const flat = [
          {level: 1, text: 'Intro', id: 'intro'},
          {level: 2, text: 'Setup', id: 'setup'},
          {level: 2, text: 'Pitfalls', id: 'pitfalls'},
          {level: 3, text: 'Edge case', id: 'edge'},
        ];
        const tree = h.adaptFlatTocToTree(flat);
        if (tree.length !== 1) throw new Error('expected synthetic root');
        if (tree[0].children.length !== 3) throw new Error('L2 count: ' + tree[0].children.length);
        if (tree[0].children[2].children.length !== 1) throw new Error('L3 nesting failed');
        if (tree[0].children[2].children[0].text !== 'Edge case') throw new Error('L3 text');
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_notes_scroll_roundtrip():
    """Notes scroll-position cache: per-course localStorage roundtrip,
    integer coercion, garbage-input rejection, and clear."""
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const s = h.createMemoryStorage();

        // No saved value → null.
        if (h.loadNotesScroll(s, 'CS182') !== null) throw new Error('expected null default');
        if (h.loadNotesScroll(s, null) !== null) throw new Error('null course should be null');

        // Save + load round-trips, integer-coerced.
        if (!h.saveNotesScroll(s, 'CS182', 1234.7)) throw new Error('save failed');
        if (h.loadNotesScroll(s, 'CS182') !== 1235) throw new Error('round failed: ' + h.loadNotesScroll(s, 'CS182'));

        // Per-course isolation.
        h.saveNotesScroll(s, 'CS285', 50);
        if (h.loadNotesScroll(s, 'CS182') !== 1235) throw new Error('CS182 clobbered: ' + h.loadNotesScroll(s, 'CS182'));
        if (h.loadNotesScroll(s, 'CS285') !== 50) throw new Error('CS285 read failed');

        // Negative / NaN / null rejected.
        if (h.saveNotesScroll(s, 'CS182', -5)) throw new Error('negative should reject');
        if (h.saveNotesScroll(s, 'CS182', NaN)) throw new Error('NaN should reject');
        if (h.saveNotesScroll(s, null, 100)) throw new Error('null course should reject');
        // Original value preserved after rejected saves.
        if (h.loadNotesScroll(s, 'CS182') !== 1235) throw new Error('rejected save corrupted value');

        // Corrupt storage value → null.
        s.setItem('nano-nlm:v1:CS182:notes-scroll-y', 'not-a-number');
        if (h.loadNotesScroll(s, 'CS182') !== null) throw new Error('corrupt value should be null');

        // clearNotesScroll wipes it.
        h.saveNotesScroll(s, 'CS182', 999);
        if (!h.clearNotesScroll(s, 'CS182')) throw new Error('clear failed');
        if (h.loadNotesScroll(s, 'CS182') !== null) throw new Error('cleared value should be null');

        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_is_latex_notes_content_discriminates_legacy_markdown():
    """R4-6 LaTeX migration: pre-R4-6 markdown caches stranded in
    localStorage render as literal '##' text in the LaTeX preview. The
    load site uses isLatexNotesContent to decide whether to keep a
    cached value or discard it (→ user sees the Generate CTA → fresh
    LaTeX gen overwrites the stale cache).

    Pin the schema check so a future LLM prompt edit that accidentally
    emits markdown for a moment doesn't silently survive a re-cache.
    """
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');

        // Empty / non-string → not LaTeX.
        if (h.isLatexNotesContent('')) throw new Error('empty must be false');
        if (h.isLatexNotesContent(null)) throw new Error('null must be false');
        if (h.isLatexNotesContent(undefined)) throw new Error('undef must be false');
        if (h.isLatexNotesContent({})) throw new Error('non-string must be false');

        // Pure markdown (pre-R4-6 cache) → must be rejected.
        const md = '# Title\\n\\n## Section\\n\\n**bold** text and `code` plus a bullet:\\n- item 1\\n- item 2\\n\\n[Source: a.pdf, Page 1/10]';
        if (h.isLatexNotesContent(md)) throw new Error('markdown must NOT pass');

        // Pure LaTeX (R4-6 cache) → must pass.
        const tex = '\\\\section{Chapter 1}\\n\\nIntro.\\n\\n\\\\subsection{Topic}\\n\\\\begin{definition}\\nA thing.\\n\\\\end{definition}';
        if (!h.isLatexNotesContent(tex)) throw new Error('latex must pass');

        // Edge case: a LaTeX doc with stray markdown-looking lines still
        // passes (the \\section marker is what matters for routing).
        const mixed = 'Some intro paragraph. ## not a real header.\\n\\\\section{Chapter}\\n';
        if (!h.isLatexNotesContent(mixed)) throw new Error('latex with stray ## must still pass');

        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_notes_scroll_listener_uses_layout_effect_for_cleanup():
    """Round 3 scroll-cache fix (Notes→Reader→Notes regression):

    The save effect that attaches the scroll listener AND captures the
    final scrollTop in its cleanup MUST be a `useLayoutEffect`, not a
    `useEffect`. Layout-effect cleanups run synchronously during the
    mutation phase (before DOM removal), so `rootRef.current.scrollTop`
    still reads the live value. Passive-effect cleanups run AFTER the
    DOM is detached — `scroller.isConnected` is false and the previous
    `isConnected`-gated flush silently no-op'd, which is why the user
    could scroll → click a citation → return to Notes → land at top.

    Pin the useLayoutEffect choice so a future refactor doesn't
    accidentally drop the timing guarantee. Also pins that the rAF
    `detached` flag still exists (defense against rAF-after-unmount).
    """
    from pathlib import Path
    src = Path(__file__).resolve().parent.parent / "frontend" / "app.jsx"
    text = src.read_text()
    # Match the save-listener block by anchoring on the function it
    # attaches: `function onScroll() { ... requestAnimationFrame(...)
    # if (detached) return ...`. The hook must be useLayoutEffect.
    import re
    m = re.search(
        r"React\.(useEffect|useLayoutEffect)\(\(\)\s*=>\s*\{[^}]*?"
        r"const scroller = rootRef\.current;[^}]*?"
        r"let detached = false;[^}]*?"
        r"function onScroll\(\)",
        text,
        re.DOTALL,
    )
    assert m, "could not locate the scroll-save effect in app.jsx"
    assert m.group(1) == "useLayoutEffect", (
        "scroll-save effect must use React.useLayoutEffect so its cleanup "
        "fires before DOM removal (regression risk: Notes→Reader→Notes "
        "lands at top if cleanup is passive). Found: " + m.group(1)
    )
    # The cleanup must NOT gate the final flush on scroller.isConnected
    # — at layout-cleanup time the node IS connected, and the previous
    # `isConnected &&` guard was a no-op that masked the bug.
    cleanup_block = text[m.start():m.start() + 3000]
    assert "scroller.isConnected" not in cleanup_block, (
        "cleanup save no longer needs the scroller.isConnected guard "
        "(it was always false in passive cleanup, which is why the bug "
        "existed); leaving it in is misleading."
    )


def test_hidden_courses_roundtrip():
    """Hidden-course set persists in localStorage and toggles atomically."""
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const s = h.createMemoryStorage();

        // Empty by default.
        const before = h.loadHiddenCourses(s);
        if (before.length !== 0) throw new Error('expected empty: ' + JSON.stringify(before));
        if (h.isCourseHidden(s, 'foo')) throw new Error('foo should not be hidden');

        // Hide one.
        let next = h.setCourseHidden(s, 'SmokeTest_A', true);
        if (next.length !== 1 || next[0] !== 'SmokeTest_A') throw new Error('hide one failed: ' + JSON.stringify(next));
        if (!h.isCourseHidden(s, 'SmokeTest_A')) throw new Error('isCourseHidden after add failed');

        // Hide a second (sorted output).
        next = h.setCourseHidden(s, 'Lecture8Test', true);
        if (JSON.stringify(next) !== '["Lecture8Test","SmokeTest_A"]')
          throw new Error('sorted output failed: ' + JSON.stringify(next));

        // Unhide one.
        next = h.setCourseHidden(s, 'SmokeTest_A', false);
        if (next.length !== 1 || next[0] !== 'Lecture8Test') throw new Error('unhide failed: ' + JSON.stringify(next));

        // filterVisibleCourses skips hidden.
        const courses = [{id:'A'},{id:'Lecture8Test'},{id:'C'}];
        const visible = h.filterVisibleCourses(courses, next);
        if (visible.map(c=>c.id).join(',') !== 'A,C') throw new Error('filter visible failed: ' + visible.map(c=>c.id));

        // clearHiddenCourses removes everything.
        h.clearHiddenCourses(s);
        if (h.loadHiddenCourses(s).length !== 0) throw new Error('clear failed');

        // Bad input — non-string entries get dropped on save.
        h.saveHiddenCourses(s, ['ok', 123, null, 'dup', 'dup']);
        const cleaned = h.loadHiddenCourses(s);
        if (JSON.stringify(cleaned) !== '["dup","ok"]')
          throw new Error('non-string filtering failed: ' + JSON.stringify(cleaned));

        // Corrupt storage falls back to empty list.
        s.setItem(h.HIDDEN_COURSES_KEY, '{not-json');
        if (h.loadHiddenCourses(s).length !== 0) throw new Error('corrupt fallback failed');

        // Non-array JSON also falls back.
        s.setItem(h.HIDDEN_COURSES_KEY, '"a-string"');
        if (h.loadHiddenCourses(s).length !== 0) throw new Error('non-array fallback failed');

        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


# ---------------------------------------------------------------------------
# review-swarm fix-all (2026-05-11): Node-smoke regressions for the
# latex-to-html shim. Each test stands the shim up via `require()`; the IIFE
# in latex-to-html.js sets `module.exports` so no DOM shim is needed.
# NanoMarkdown is optional (the shim falls back to a built-in escapeHtml +
# no-op stashMath when the global is absent), so we can exercise the
# rendering pipeline directly under Node.
# ---------------------------------------------------------------------------


def test_latex_nested_env_renders_with_styles():
    """Stage-3 recursive env stash + Stage-8 looped restore: a `\\begin{proof}`
    nested inside `\\begin{theorem}` must produce BOTH the `thm-theorem` and
    `thm-proof` div classes. The pre-fix multi-pass loop only caught the
    outer env; the inner `\\begin{proof}` survived as literal escaped text
    inside renderInnerFragment's output."""
    script = textwrap.dedent(
        """
        const NL = require('./frontend/latex-to-html.js');
        const html = NL.latexToHtml('\\\\begin{theorem}outer body \\\\begin{proof}qed\\\\end{proof}\\\\end{theorem}');
        if (!html.includes('thm-box thm-theorem')) throw new Error('outer theorem class missing: ' + html);
        if (!html.includes('thm-box thm-proof')) throw new Error('nested proof class missing (regression): ' + html);
        // Proof box must live INSIDE the theorem box, not adjacent to it.
        const theoremIdx = html.indexOf('thm-theorem');
        const proofIdx = html.indexOf('thm-proof');
        if (theoremIdx < 0 || proofIdx < 0 || proofIdx < theoremIdx) {
          throw new Error('proof should appear after theorem in the html: ' + html);
        }
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_latex_extract_toc_strips_inline_macros():
    """extractTOC must reduce `\\subsection{\\texttt{leaq}：地址计算指令}` to
    plain text `leaq：地址计算指令` so the sidebar shows readable titles
    instead of literal LaTeX macros. After the tree refactor a bare
    \\subsection without an enclosing file-wrapper section gets a
    synthetic L1 parent, so the cleaned title lives at tree[0].children[0]."""
    script = textwrap.dedent(
        """
        const NL = require('./frontend/latex-to-html.js');
        const tree = NL.extractTOC('\\\\subsection{\\\\texttt{leaq}：地址计算指令}');
        if (tree.length !== 1) throw new Error('expected 1 synthetic L1, got ' + tree.length);
        const leaf = tree[0].children[0];
        if (!leaf) throw new Error('synthetic L1 missing child: ' + JSON.stringify(tree[0]));
        if (leaf.text !== 'leaq：地址计算指令') {
          throw new Error('inline macro not stripped: ' + JSON.stringify(leaf));
        }
        if (leaf.text.startsWith('\\\\texttt')) {
          throw new Error('leading \\\\texttt survived: ' + leaf.text);
        }
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_latex_cite_chip_normalises_colon_to_comma():
    """`\\cite{file.pdf:Page 4/50}` must emit a chip whose data-cite is in
    canonical `[Source: file.pdf, Page 4/50]` form — the comma split is the
    contract resolveCitationNavigation parses in study-state.js."""
    script = textwrap.dedent(
        """
        const NL = require('./frontend/latex-to-html.js');
        const html = NL.latexToHtml('\\\\cite{file.pdf:Page 4/50}');
        if (!html.includes('data-cite="[Source: file.pdf, Page 4/50]"')) {
          throw new Error('comma form not produced: ' + html);
        }
        if (html.includes('data-cite="[Source: file.pdf:Page 4/50]"')) {
          throw new Error('legacy colon form leaked into data-cite: ' + html);
        }
        // The display label (chip text) should also use the comma form.
        if (!html.includes('>file.pdf, Page 4/50</button>')) {
          throw new Error('chip label not normalised: ' + html);
        }
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_latex_cite_chip_inside_env_populates():
    """v3 #4 regression: a `\\cite` inside `\\begin{theorem}` must still
    produce a chip with a non-empty data-cite attribute. The pre-v3 code
    recursively called latexToHtml inside renderInnerFragment with a fresh
    citeBuf, which looked CITE_n up in an empty buffer and emitted
    `[Source: ]` chips. Confirms renderInnerFragment now preserves the
    placeholder for the outer Stage-9 sweep."""
    script = textwrap.dedent(
        """
        const NL = require('./frontend/latex-to-html.js');
        const html = NL.latexToHtml('\\\\begin{theorem}body \\\\cite{x.pdf:p1}\\\\end{theorem}');
        if (!html.includes('thm-box thm-theorem')) throw new Error('theorem missing: ' + html);
        // The chip should be present with the actual filename + location,
        // not the empty `[Source: ]` regression form.
        if (!html.includes('data-cite="[Source: x.pdf, p1]"')) {
          throw new Error('cite-in-env did not populate data-cite: ' + html);
        }
        if (html.includes('data-cite="[Source: ]"')) {
          throw new Error('empty data-cite leaked: ' + html);
        }
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


# ── Background-task upload (2026-05-16) ──────────────────────────────────


def test_api_js_has_start_upload_and_get_status():
    """The synchronous-streaming `uploadFiles` was replaced with a fire-and-poll
    pair: `startUpload` (returns {task_id}) + `getUploadStatus` (polls).
    Pin the surface so the old streaming helper can't sneak back in."""
    src = Path("frontend/api.js").read_text(encoding="utf-8")
    assert "async startUpload(" in src, "startUpload missing"
    assert "async getUploadStatus(" in src, "getUploadStatus missing"
    # Legacy `async uploadFiles(...)` definition must be gone (a passing
    # mention in a comment is fine; an actual function or call is not).
    assert "async uploadFiles(" not in src, "legacy uploadFiles definition must be removed"
    assert "API.uploadFiles(" not in src, "legacy uploadFiles call must be removed"
    assert "/upload/status/" in src, "polling endpoint path missing"


def test_api_js_start_upload_returns_json_no_streaming():
    """startUpload must return the JSON {task_id, course_id} body directly,
    not consume an NDJSON stream."""
    src = Path("frontend/api.js").read_text(encoding="utf-8")
    # Slice the startUpload body
    idx = src.index("async startUpload(")
    end = src.index("\n  },", idx)
    body = src[idx:end]
    assert "res.json()" in body, "must return JSON body"
    assert "getReader" not in body, "must not stream"
    assert "TextDecoder" not in body, "must not stream"


def test_api_js_get_upload_status_handles_404():
    """A 404 from the polling endpoint means the server forgot the task —
    the helper must return `null` (caller sentinel) instead of throwing."""
    src = Path("frontend/api.js").read_text(encoding="utf-8")
    idx = src.index("async getUploadStatus(")
    end = src.index("\n  },", idx)
    body = src[idx:end]
    assert "res.status === 404" in body
    assert "return null" in body


def test_app_jsx_uses_localstorage_upload_task_key():
    """app.jsx must persist the active task_id under the agreed key
    `nano-nlm:v1:upload-task:<courseId>` so resume-on-mount works."""
    src = Path("frontend/app.jsx").read_text(encoding="utf-8")
    assert "nano-nlm:v1:upload-task:" in src
    # Verify the polling helper is wired in.
    assert "API.getUploadStatus(" in src
    assert "API.startUpload(" in src
    # And the legacy NDJSON helper is fully gone from app.jsx.
    assert "API.uploadFiles(" not in src


def test_app_jsx_localstorage_upload_task_lifecycle():
    """review-swarm M4 (2026-05-16): pin the full localStorage lifecycle —
    write on upload start, remove on terminal status, read on resume.
    The previous grep-only assertion let a future refactor that deletes
    any one of the three points pass silently.
    """
    src = Path("frontend/app.jsx").read_text(encoding="utf-8")
    prefix = "nano-nlm:v1:upload-task:"
    occurrences = src.count(prefix)
    # 1 write (setItem in runUpload), ≥ 3 removeItem (done branch +
    # 404 branch + inactive-cleanup branch + parse-fail branches), 1
    # read (getItem in resume) ⇒ ≥ 5 references. We use ≥ 4 as a
    # robustness floor; the actual count will fluctuate slightly as
    # the file evolves.
    assert occurrences >= 4, (
        f"upload-task key referenced only {occurrences}x — "
        f"expected write + remove(s) + read. A refactor likely broke "
        f"the lifecycle."
    )
    # Specific anchors that MUST exist.
    assert ".setItem(" in src and prefix in src, "missing setItem(upload-task:…)"
    assert ".removeItem(" in src, "missing removeItem(upload-task:…)"
    assert ".getItem(" in src, "missing getItem(upload-task:…)"
    # Resume-on-mount helper is the read site.
    assert "_resumePendingUploads" in src
    # Inactive-cleanup helper (H3) must exist for the non-active branch.
    assert "_scheduleInactiveUploadCleanup" in src, (
        "review-swarm H3: non-active in-flight candidates need a "
        "cleanup recheck so their localStorage hint doesn't leak"
    )


def test_app_jsx_pollref_app_unmount_cleanup():
    """review-swarm H2 (2026-05-16): the App-level useEffect that clears
    pollRef on unmount must exist; otherwise StrictMode / HMR / Vite
    migrations leak the 1.5s poll interval. Pin by source so a refactor
    that drops the cleanup tears the build.
    """
    src = Path("frontend/app.jsx").read_text(encoding="utf-8")
    # Look for the empty-deps cleanup that grabs pollRef.
    # The exact pattern: `useEffect(() => () => { if (pollRef.current)`.
    assert "if (pollRef.current)" in src
    # The empty deps array marker `}, []);` near the cleanup is the
    # contract — fires on real unmount only, not on every render.
    # We check that the cleanup expression for pollRef is followed
    # somewhere by an empty deps tuple (multiple useEffects in the
    # file use `}, []);` so this is a soft pin — the previous source
    # presence check above already pins the cleanup body itself.
    assert "review-swarm H2" in src, (
        "H2 fix-marker missing — pollRef App-unmount cleanup should "
        "carry the review-swarm H2 comment so a future grep finds it"
    )


def test_app_jsx_double_click_upload_guard():
    """review-swarm M1 (2026-05-16): onStartUpload must early-return when
    a non-terminal upload is already in flight, so a double-click can't
    overwrite the localStorage key with a second task_id (orphaning the
    first task server-side).
    """
    src = Path("frontend/app.jsx").read_text(encoding="utf-8")
    assert "review-swarm M1" in src
    # The guard body must check both processing presence + non-terminal.
    assert "!processing.done" in src
    assert "!processing.errorStage" in src


def test_app_jsx_poll_failure_cutoff():
    """review-swarm M2 (2026-05-16): the 1.5s poll must not hammer the
    endpoint forever on a sustained 5xx outage. After MAX_FAILURES
    consecutive transient errors, clearInterval + surface a transport
    error to the user.
    """
    src = Path("frontend/app.jsx").read_text(encoding="utf-8")
    assert "review-swarm M2" in src
    assert "MAX_FAILURES" in src
    assert "failures >= MAX_FAILURES" in src


def test_app_jsx_upload_course_picker_replaces_prompt_and_confirm():
    """review-swarm fix-all (2026-05-20): the upload course-name + engine +
    file-picker chain must all flow through CoursePickerModal in a single
    user-gesture window. The old chain `prompt()` → `confirm()` →
    `document.createElement('input').click()` lost transient activation on
    Chrome and silently dropped the file dialog. Negative-pin the
    onStartUpload function body so the legitimate `window.prompt` in
    handleDeleteCourse (destructive-action guard) doesn't accidentally
    satisfy the check.
    """
    src = Path("frontend/app.jsx").read_text(encoding="utf-8")
    assert "function CoursePickerModal(" in src
    assert "function pickCourseAndFiles(" in src
    assert "await pickCourseAndFiles()" in src
    upload_fn = src.split("async function onStartUpload()", 1)[1]
    upload_fn = upload_fn.split("\n  function ", 1)[0]
    upload_fn = upload_fn.split("\n  async function ", 1)[0]
    assert "window.prompt" not in upload_fn, (
        "onStartUpload regressed to window.prompt — the picker modal is the "
        "intended UI for course-name entry"
    )
    assert " prompt(" not in upload_fn
    # The MinerU confirm() + document.createElement('input') chain must NOT
    # come back — it loses Chrome's transient activation token in the await
    # gap and silently drops the OS file dialog.
    assert "window.confirm" not in upload_fn
    assert "document.createElement(\"input\")" not in upload_fn
    assert "document.createElement('input')" not in upload_fn


def test_app_jsx_course_picker_duplicate_and_validation_guard():
    """review-swarm fix-all #H2 + #M1: duplicate detection must be case-
    and whitespace-insensitive over BOTH id and name; the new-name input
    must mirror server-side COURSE_ID_PATTERN so RTL / zero-width / `..`
    / oversized values are rejected before they pollute localStorage."""
    src = Path("frontend/app.jsx").read_text(encoding="utf-8")
    assert "COURSE_ID_RE" in src
    assert "function isValidCourseId(" in src
    assert "trim().toLowerCase()" in src
    assert "existingKeys" in src
    assert "duplicateNew" in src
    assert "newInputValid" in src


def test_app_jsx_course_picker_mount_uses_visible_courses_and_default_id():
    """review-swarm fix-all #H1: modal must be fed the hidden-filtered
    course list AND defaultId (not defaultName) so the chip highlight and
    the resolved value both align with the course_id used by the upload
    pipeline."""
    src = Path("frontend/app.jsx").read_text(encoding="utf-8")
    assert "<CoursePickerModal" in src
    assert "courses={visibleCourses}" in src
    assert "defaultId={activeCourse" in src
    # Sanity: the legacy `defaultName=` prop must be gone, otherwise the
    # chip highlight silently goes back to comparing against `activeCourse`
    # under the wrong name.
    assert "defaultName=" not in src
    # Engine + files must be wired through the modal's onPick (the modal owns
    # the OS file dialog now — picking files in `onStartUpload` post-await
    # would lose user activation).
    assert "defaultEngine={uploadEngine}" in src
    assert "onPick={(courseId, files, engine)" in src


def test_processing_jsx_handles_nested_progress_shape():
    """Background-task status returns `stages.chunking.progress` (object),
    not a flat number. Processing component must tolerate both."""
    src = Path("frontend/processing.jsx").read_text(encoding="utf-8")
    assert "v.progress" in src, "Processing must read nested {progress, detail} shape"
