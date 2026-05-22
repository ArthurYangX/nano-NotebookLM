/* global React, API */
// Exam Prep — closed-loop, self-evolving exam preparation.
//
// Three internal views (controlled by `view` state):
//   "topics"  — bank summary + per-topic mastery progress (entry point)
//   "quiz"    — active quiz session (sampled non-mastered questions)
//   "result"  — graded review + variant-generation summary
//
// All data lives in the backend (`artifacts/courses/<id>/exam_bank.json`);
// this component is a thin shell over the /api/exam-prep/* endpoints. The
// only client-side state is "which questions are we answering right now"
// and "what did the user pick" — the bank itself is the source of truth.

const { useState: useEP, useEffect: useEPEffect, useCallback: useEPCallback, useRef: useEPRef } = React;

function ExamPrep({ activeCourse, userLang }) {
  const t = (k, vars) => window.I18N.t(k, userLang || "en", vars);
  const [view, setView] = useEP("topics"); // topics | quiz | result
  const [loading, setLoading] = useEP(false);
  const [loadingLabel, setLoadingLabel] = useEP("");
  const [elapsedSec, setElapsedSec] = useEP(0);
  const elapsedTimerRef = useEPRef(null);
  const [error, setError] = useEP("");
  const [bankView, setBankView] = useEP(null); // { topics: [...], total_mastered, ...}
  const [quizQuestions, setQuizQuestions] = useEP([]);
  const [quizScope, setQuizScope] = useEP(null); // null = all, or topic_id
  const [answers, setAnswers] = useEP({}); // qid -> user_answer
  const [graded, setGraded] = useEP(null);  // result payload from submit

  // Live elapsed-seconds counter while `loading` is true. Without this the
  // user sees a silent "Working…" during the 5-45 s variant-gen window and
  // can't tell whether the page is alive or the request has hung.
  function startBusy(label) {
    setLoading(true);
    setLoadingLabel(label || t("exam.busy.working"));
    setElapsedSec(0);
    if (elapsedTimerRef.current) clearInterval(elapsedTimerRef.current);
    const t0 = Date.now();
    // Display granularity is whole seconds, so a 1s interval is enough.
    // The earlier 500ms cadence doubled the render-attempt rate during
    // the loading window for zero visible benefit.
    elapsedTimerRef.current = setInterval(() => {
      setElapsedSec(Math.round((Date.now() - t0) / 1000));
    }, 1000);
  }
  function stopBusy() {
    setLoading(false);
    setLoadingLabel("");
    setElapsedSec(0);
    if (elapsedTimerRef.current) {
      clearInterval(elapsedTimerRef.current);
      elapsedTimerRef.current = null;
    }
  }
  // Cleanup on unmount so we don't leak the timer.
  useEPEffect(() => () => {
    if (elapsedTimerRef.current) clearInterval(elapsedTimerRef.current);
  }, []);

  // Initial load
  useEPEffect(() => {
    if (!activeCourse) { setBankView(null); return; }
    refresh();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeCourse]);

  // fix-all v1 M10: snapshot the active course at the moment a request
  // fires; if it changes mid-flight (user clicked a different course),
  // discard the result so we don't render B's data against C's selection.
  const refresh = useEPCallback(async () => {
    if (!activeCourse) return;
    const myActive = activeCourse;
    startBusy(t("exam.busy.loading_bank")); setError("");
    try {
      const data = await API.examPrepView(myActive);
      if (myActive !== activeCourse) return;
      setBankView(data.view || null);
    } catch (e) {
      if (myActive !== activeCourse) return;
      setError(e.message || t("exam.error.failed_load_bank"));
    } finally {
      if (myActive === activeCourse) stopBusy();
    }
  }, [activeCourse]);

  // fix-all v1 L9: refuse to launch a second concurrent action while one
  // is in flight. Pre-fix, a double-click on Submit/Re-extract could fire
  // two requests, doubling LLM cost AND racing H2's per-course lock.
  function busy() { return loading; }

  async function handlePlan(force = false) {
    if (busy()) return;
    const myActive = activeCourse;
    startBusy(t("exam.busy.extracting")); setError("");
    try {
      const data = await API.examPrepPlan(myActive, { force, userLang });
      if (myActive !== activeCourse) return;
      setBankView(data.view || null);
      // H4: surface what was preserved / archived after a re-extract.
      if (force && (data.orphan_question_count || data.migrated_topic_count)) {
        setError(t("exam.info.re_extract_done", {
          migrated: data.migrated_topic_count || 0,
          orphans: data.orphan_question_count || 0,
        }));
      }
    } catch (e) {
      if (myActive !== activeCourse) return;
      setError(e.message || t("exam.error.failed_extract"));
    } finally {
      if (myActive === activeCourse) stopBusy();
    }
  }

  async function startQuiz(topicId = null) {
    if (busy()) return;
    const myActive = activeCourse;
    startBusy(t("exam.busy.sampling")); setError(""); setAnswers({}); setGraded(null);
    try {
      const data = await API.examPrepNextQuiz(myActive, {
        size: 8,
        topicIds: topicId ? [topicId] : null,
        userLang,
      });
      if (myActive !== activeCourse) return;
      if (!data.questions || data.questions.length === 0) {
        // M6: surface the distinct reason so the user knows whether to retry
        // (generation_failed), shrug it off (all_mastered), or check ingest.
        const reason = data.reason || "no_questions";
        if (reason === "generation_failed") {
          setError(t("exam.error.gen_failed"));
        } else if (reason === "all_mastered") {
          setError(t("exam.error.all_mastered"));
        } else {
          setError(t("exam.error.no_questions"));
        }
        return;
      }
      setQuizQuestions(data.questions);
      setQuizScope(topicId);
      setView("quiz");
    } catch (e) {
      if (myActive !== activeCourse) return;
      setError(e.message || t("exam.error.failed_start"));
    } finally {
      if (myActive === activeCourse) stopBusy();
    }
  }

  async function handleSubmit() {
    if (busy()) return;
    if (Object.keys(answers).length === 0) {
      setError(t("exam.error.answer_at_least_one"));
      return;
    }
    const myActive = activeCourse;
    startBusy(t("exam.busy.grading")); setError("");
    try {
      const data = await API.examPrepSubmit(myActive, answers, { userLang });
      if (myActive !== activeCourse) return;
      setGraded(data);
      setBankView(data.view || bankView);
      setView("result");
    } catch (e) {
      if (myActive !== activeCourse) return;
      setError(e.message || t("exam.error.failed_submit"));
    } finally {
      if (myActive === activeCourse) stopBusy();
    }
  }

  async function handleReset() {
    if (!window.confirm(t("exam.confirm.reset"))) return;
    startBusy(t("exam.busy.resetting"));
    try {
      await API.examPrepReset(activeCourse);
      setBankView(null);
      setView("topics");
      setQuizQuestions([]); setAnswers({}); setGraded(null);
    } catch (e) {
      setError(e.message || t("exam.error.failed_reset"));
    }
    stopBusy();
  }

  if (!activeCourse) {
    return (
      <div className="reader-body exam-prep-wrap">
        <div className="exam-prep-empty">
          <h2>{t("exam.title")}</h2>
          <p>{t("exam.empty_select_course")}</p>
        </div>
      </div>
    );
  }

  const hasTopics = bankView && bankView.topic_count > 0;

  return (
    <div className="reader-body exam-prep-wrap" data-screen-label={t("exam.title")}>
      <header className="exam-prep-header">
        <div>
          <h2>{t("exam.title")} · {activeCourse}</h2>
          {bankView && (
            <div className="exam-prep-overall">
              <span>{t("exam.stats.questions_mastered", { done: bankView.total_mastered, total: bankView.total_questions })}</span>
              <span className="dot">·</span>
              <span>{t("exam.stats.topics_mastered", { done: bankView.mastered_topics, total: bankView.topic_count })}</span>
              {bankView.total_attempts > 0 && (
                <React.Fragment>
                  <span className="dot">·</span>
                  <span>{t("exam.stats.attempts", { n: bankView.total_attempts })}</span>
                  <span className="dot">·</span>
                  <span className="mono">{t("exam.stats.correct_pct", { pct: Math.round((bankView.overall_correct_rate || 0) * 100) })}</span>
                </React.Fragment>
              )}
            </div>
          )}
        </div>
        <div className="exam-prep-actions">
          {hasTopics && view !== "quiz" && (
            <button
              className="btn ghost"
              onClick={() => {
                // fix-all v1 H4: re-extract may rename topics → name-drift
                // would have dropped questions; we now preserve by normalized
                // name and archive orphans, but the LLM cost + UX disruption
                // still warrants a confirm.
                const ok = window.confirm(t("exam.confirm.re_extract"));
                if (ok) handlePlan(true);
              }}
              disabled={loading}
              title={t("exam.tooltip.re_extract")}
            >
              {t("exam.action.re_extract")}
            </button>
          )}
          {hasTopics && (
            <button className="btn ghost danger" onClick={handleReset} disabled={loading}>
              {t("exam.action.reset")}
            </button>
          )}
        </div>
      </header>

      {error && <div className="error-banner">{error}</div>}

      {/* Empty state — no topics yet */}
      {!loading && !hasTopics && view === "topics" && (
        <div className="exam-prep-empty">
          <p>{t("exam.empty_no_topics")}</p>
          <button className="btn primary" onClick={() => handlePlan(false)} disabled={loading}>
            {t("exam.action.extract_topics")}
          </button>
        </div>
      )}

      {loading && (
        <div className="exam-prep-loading">
          <div className="exam-prep-loading-label">{loadingLabel || t("exam.busy.working")}</div>
          <div className="exam-prep-loading-elapsed mono">
            {t("exam.busy.elapsed", { n: elapsedSec })}{elapsedSec >= 60 ? t("exam.busy.elapsed_long_hint") : ""}
          </div>
        </div>
      )}

      {/* Topics list */}
      {!loading && hasTopics && view === "topics" && (
        <ExamPrepTopics
          t={t}
          bankView={bankView}
          onStartMixed={() => startQuiz(null)}
          onStartTopic={(tid) => startQuiz(tid)}
        />
      )}

      {/* Active quiz */}
      {view === "quiz" && (
        <ExamPrepQuiz
          t={t}
          questions={quizQuestions}
          scope={quizScope}
          answers={answers}
          onAnswer={(qid, ans) => setAnswers(a => ({ ...a, [qid]: ans }))}
          onSubmit={handleSubmit}
          onCancel={() => setView("topics")}
          loading={loading}
        />
      )}

      {/* Graded result */}
      {view === "result" && graded && (
        <ExamPrepResult
          t={t}
          graded={graded}
          questions={quizQuestions}
          answers={answers}
          onContinue={() => {
            setView("topics");
            setGraded(null);
            // Force re-read of the bank from disk so the topic cards reflect
            // the latest attempt counts / correct rates / mastery flips.
            // The submit response carried `data.view` which we already set
            // into bankView, but a fresh GET is cheap insurance.
            refresh();
          }}
          onAgain={() => startQuiz(quizScope)}
        />
      )}
    </div>
  );
}

function ExamPrepTopics({ t, bankView, onStartMixed, onStartTopic }) {
  // fix-all v3 (2026-05-22): `t` is the i18n helper passed from parent;
  // topic objects inside the filter / map are aliased to `tp` to avoid
  // shadowing it.
  const liveTopics = (bankView.topics || []).filter(tp => !tp.is_archived);
  const archivedTopics = (bankView.topics || []).filter(tp => tp.is_archived);
  return (
    <div className="exam-prep-topics">
      <div className="exam-prep-cta-row">
        <button className="btn primary" onClick={onStartMixed}>
          {t("exam.action.start_mixed")}
        </button>
      </div>
      <div className="topic-grid">
        {liveTopics.map(tp => {
          const pct = Math.round((tp.mastery_ratio || 0) * 100);
          return (
            <div key={tp.id} className={"topic-card" + (tp.is_mastered ? " mastered" : "")}>
              <div className="topic-card-head">
                <h3>{tp.name}</h3>
                {tp.is_mastered && <span className="topic-mastered-chip">{t("exam.topic.mastered_chip")}</span>}
              </div>
              <div className="topic-meta">
                <span className="mono">{t("exam.topic.weight", { pct: Math.round((tp.weight || 0) * 100) })}</span>
                <span className="dot">·</span>
                <span>{t("exam.topic.mastered_count", { done: tp.mastered_count, total: tp.question_count })}</span>
              </div>
              {tp.attempt_count > 0 && (
                <div className="topic-meta topic-attempts">
                  <span>{t("exam.topic.attempts", { n: tp.attempt_count })}</span>
                  <span className="dot">·</span>
                  <span className={"topic-correct-rate " + (tp.correct_rate >= 0.7 ? "good" : tp.correct_rate >= 0.4 ? "mid" : "bad")}>
                    {t("exam.topic.correct_rate", { pct: Math.round((tp.correct_rate || 0) * 100) })}
                  </span>
                </div>
              )}
              <div className="topic-progress">
                <div className="topic-progress-bar" style={{ width: pct + "%" }} />
              </div>
              <button
                className="btn ghost topic-quiz-btn"
                onClick={() => onStartTopic(tp.id)}
                title={tp.is_mastered ? t("exam.topic.re_quiz_tip") : t("exam.topic.start_quiz_tip", { name: tp.name })}
              >
                {tp.is_mastered ? t("exam.topic.review_btn") : t("exam.topic.start_btn")}
              </button>
            </div>
          );
        })}
      </div>
      {archivedTopics.length > 0 && (
        <div className="topic-archive-note">
          {t("exam.archive.label", { n: archivedTopics.length })}
        </div>
      )}
    </div>
  );
}

function ExamPrepQuiz({ t, questions, scope, answers, onAnswer, onSubmit, onCancel, loading }) {
  const answered = Object.keys(answers).filter(k => answers[k] != null && answers[k] !== "").length;
  return (
    <div className="exam-prep-quiz">
      <div className="exam-prep-quiz-head">
        <div>
          <strong>{t("exam.quiz.title", { n: questions.length })}</strong>
          {scope && <span className="mono">{t("exam.quiz.scoped_topic")}</span>}
        </div>
        <div className="exam-prep-quiz-progress">
          <span className="mono">{answered} / {questions.length}</span>
          <div className="topic-progress" style={{ width: 120 }}>
            <div className="topic-progress-bar" style={{ width: (questions.length ? (answered / questions.length) * 100 : 0) + "%" }} />
          </div>
        </div>
      </div>

      {questions.map((q, i) => (
        <div className="exam-question" key={q.id}>
          <div className="exam-q-num">
            <span>Q{i + 1}.</span>
            <span className="mono dim">{q.topic_name}</span>
            <span className="mono dim">{q.type}</span>
            {q.difficulty && <span className="mono dim">{q.difficulty}</span>}
          </div>
          <p className="exam-q-prompt">{q.prompt}</p>
          {q.options && Array.isArray(q.options) ? (
            <div className="exam-q-options">
              {q.options.map((opt, j) => {
                const letter = (typeof opt === "string" && opt) ? opt.charAt(0).toUpperCase() : String.fromCharCode(65 + j);
                const selected = answers[q.id] === letter;
                return (
                  <label key={j} className={"exam-q-option" + (selected ? " selected" : "")}>
                    <input
                      type="radio"
                      name={"q_" + q.id}
                      checked={selected}
                      onChange={() => onAnswer(q.id, letter)}
                    />
                    <span>{opt}</span>
                  </label>
                );
              })}
            </div>
          ) : (
            <textarea
              className="exam-q-textarea"
              rows={3}
              placeholder={t("exam.quiz.placeholder")}
              value={answers[q.id] || ""}
              onChange={e => onAnswer(q.id, e.target.value)}
            />
          )}
        </div>
      ))}

      <div className="exam-prep-quiz-footer">
        <button className="btn ghost" onClick={onCancel} disabled={loading}>{t("exam.quiz.back")}</button>
        <button className="btn primary" onClick={onSubmit} disabled={loading || answered === 0}>
          {t("exam.quiz.submit", { n: answered })}
        </button>
      </div>
    </div>
  );
}

function ExamPrepResult({ t, graded, questions, answers, onContinue, onAgain }) {
  // fix-all v3 (2026-05-22): `t` is now required — previously this
  // component referenced `t(...)` from its parent's closure, which
  // worked when the file was a single component but broke once these
  // were factored into separate top-level functions. The exam.variant_*
  // hint branch threw ReferenceError after Grade (the variants_pending
  // path always fires on submit), crashing the result page render.
  const total = graded.graded.length;
  const right = graded.graded.filter(g => g.correct).length;
  const wrong = total - right;
  const variants = graded.variants_added || {};
  const variantCount = Object.values(variants).reduce((s, n) => s + n, 0);
  // 2026-05-13: variant generation moved to a fire-and-forget background
  // task so submit returns in ~50ms instead of waiting 8-15s for the LLM
  // calls. `variants_added` is now always empty in the immediate response
  // — use `variants_pending` + `expected_variant_count` to surface that
  // new questions are being generated and will appear on the next quiz.
  const variantsPending = !!graded.variants_pending;
  const expectedVariants = Number(graded.expected_variant_count || 0);
  const qById = {};
  questions.forEach(q => { qById[q.id] = q; });

  return (
    <div className="exam-prep-result">
      <div className="exam-result-summary">
        <div className="exam-result-stat correct">
          <b>{right}</b>
          <span>{t("exam.result.correct")}</span>
        </div>
        <div className="exam-result-stat wrong">
          <b>{wrong}</b>
          <span>{t("exam.result.wrong")}</span>
        </div>
        <div className="exam-result-stat">
          <b>{Math.round(total ? right * 100 / total : 0)}%</b>
          <span>{t("exam.result.score")}</span>
        </div>
        {variantCount > 0 && (
          <div className="exam-result-stat variants" title={t("exam.result.fresh_variants_tip", { n: graded.variant_budget_per_topic })}>
            <b>+{variantCount}</b>
            <span>{t("exam.result.fresh_variants")}</span>
          </div>
        )}
        {variantCount === 0 && variantsPending && expectedVariants > 0 && (
          <div
            className="exam-result-stat variants"
            title={t("exam.variant_brewing_tip")}
          >
            <b>~{expectedVariants}</b>
            <span>{t("exam.variant_brewing")}</span>
          </div>
        )}
      </div>

      <div className="exam-result-list">
        {graded.graded.map((g, i) => {
          const q = qById[g.question_id] || {};
          const userAns = answers[g.question_id];
          return (
            <div key={g.question_id} className={"exam-result-item" + (g.correct ? " correct" : " wrong")}>
              <div className="exam-result-head">
                <span>{g.correct ? "✓" : "✗"} Q{i + 1}</span>
                <span className="mono dim">{g.topic_name}</span>
              </div>
              <p className="exam-q-prompt">{q.prompt}</p>
              <div className="exam-result-detail">
                <div><strong>{t("exam.result.your_answer")}</strong> {userAns || <em>{t("exam.result.empty_answer")}</em>}</div>
                {!g.correct && (
                  <div><strong>{t("exam.result.expected")}</strong> {g.expected}</div>
                )}
                {g.explanation && (
                  <div className="dim"><strong>{t("exam.result.why")}</strong> {g.explanation}</div>
                )}
              </div>
            </div>
          );
        })}
      </div>

      <div className="exam-prep-quiz-footer">
        <button className="btn ghost" onClick={onContinue}>{t("exam.result.back_topics")}</button>
        <button className="btn primary" onClick={onAgain}>{t("exam.result.another_round")}</button>
      </div>
    </div>
  );
}

Object.assign(window, { ExamPrep });
