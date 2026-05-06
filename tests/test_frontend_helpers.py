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
