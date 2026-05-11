"""Contract tests for frontend-only study state helpers.

The app has no build step, so these tests execute plain helper JavaScript with
Node and keep React/Babel out of the test path.
"""

from __future__ import annotations

import subprocess
import textwrap


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


def test_observability_status_happy():
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const status = h.formatStatusBar({backends:['codex'], latency_ms:{search_p50:120, chat_p50:4100}, usage:{total_cost:0.12}});
        if (!status.text.includes('codex')) throw new Error('backend missing');
        if (!status.text.includes('120ms')) throw new Error('latency missing');
        if (!status.ok) throw new Error('healthy status should be ok');
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_observability_status_timeout():
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const status = h.formatStatusBar(null);
        if (!status.degraded) throw new Error('missing degraded flag');
        if (!status.text.includes('degraded')) throw new Error('missing degraded text');
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
    instead of literal LaTeX macros."""
    script = textwrap.dedent(
        """
        const NL = require('./frontend/latex-to-html.js');
        const toc = NL.extractTOC('\\\\subsection{\\\\texttt{leaq}：地址计算指令}');
        if (toc.length !== 1) throw new Error('expected 1 entry, got ' + toc.length);
        if (toc[0].text !== 'leaq：地址计算指令') {
          throw new Error('inline macro not stripped: ' + JSON.stringify(toc[0]));
        }
        if (toc[0].text.startsWith('\\\\texttt')) {
          throw new Error('leading \\\\texttt survived: ' + toc[0].text);
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
