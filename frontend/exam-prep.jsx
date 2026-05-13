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
    setLoadingLabel(label || "Working…");
    setElapsedSec(0);
    if (elapsedTimerRef.current) clearInterval(elapsedTimerRef.current);
    const t0 = Date.now();
    elapsedTimerRef.current = setInterval(() => {
      setElapsedSec(Math.round((Date.now() - t0) / 1000));
    }, 500);
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
    startBusy("Loading exam bank…"); setError("");
    try {
      const data = await API.examPrepView(myActive);
      if (myActive !== activeCourse) return;
      setBankView(data.view || null);
    } catch (e) {
      if (myActive !== activeCourse) return;
      setError(e.message || "Failed to load exam bank");
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
    startBusy("Extracting exam topics from course materials…"); setError("");
    try {
      const data = await API.examPrepPlan(myActive, { force, userLang });
      if (myActive !== activeCourse) return;
      setBankView(data.view || null);
      // H4: surface what was preserved / archived after a re-extract.
      if (force && (data.orphan_question_count || data.migrated_topic_count)) {
        setError(
          `Re-extract complete · ${data.migrated_topic_count || 0} topic(s) carried questions forward · ${data.orphan_question_count || 0} orphan questions archived (visible in a "[archive] ..." topic).`
        );
      }
    } catch (e) {
      if (myActive !== activeCourse) return;
      setError(e.message || "Topic extraction failed");
    } finally {
      if (myActive === activeCourse) stopBusy();
    }
  }

  async function startQuiz(topicId = null) {
    if (busy()) return;
    const myActive = activeCourse;
    startBusy("Sampling questions from the bank…"); setError(""); setAnswers({}); setGraded(null);
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
          setError("Question generation failed for this topic. Please retry — the LLM may have timed out or returned malformed JSON.");
        } else if (reason === "all_mastered") {
          setError("All questions in scope are already mastered. Try re-extracting topics or pick a different topic.");
        } else {
          setError("No questions available — try seeding this topic or check the course KB has content.");
        }
        return;
      }
      setQuizQuestions(data.questions);
      setQuizScope(topicId);
      setView("quiz");
    } catch (e) {
      if (myActive !== activeCourse) return;
      setError(e.message || "Failed to start quiz");
    } finally {
      if (myActive === activeCourse) stopBusy();
    }
  }

  async function handleSubmit() {
    if (busy()) return;
    if (Object.keys(answers).length === 0) {
      setError("Please answer at least one question before submitting.");
      return;
    }
    const myActive = activeCourse;
    startBusy("Grading + generating fresh variants for any wrong topics…"); setError("");
    try {
      const data = await API.examPrepSubmit(myActive, answers, { userLang });
      if (myActive !== activeCourse) return;
      setGraded(data);
      setBankView(data.view || bankView);
      setView("result");
    } catch (e) {
      if (myActive !== activeCourse) return;
      setError(e.message || "Submit failed");
    } finally {
      if (myActive === activeCourse) stopBusy();
    }
  }

  async function handleReset() {
    if (!window.confirm("Wipe the entire exam bank? You'll need to re-extract topics from scratch.")) return;
    startBusy("Resetting bank…");
    try {
      await API.examPrepReset(activeCourse);
      setBankView(null);
      setView("topics");
      setQuizQuestions([]); setAnswers({}); setGraded(null);
    } catch (e) {
      setError(e.message || "Reset failed");
    }
    stopBusy();
  }

  if (!activeCourse) {
    return (
      <div className="reader-body exam-prep-wrap">
        <div className="exam-prep-empty">
          <h2>Exam Prep</h2>
          <p>Select a course from the sidebar to begin exam preparation.</p>
        </div>
      </div>
    );
  }

  const hasTopics = bankView && bankView.topic_count > 0;

  return (
    <div className="reader-body exam-prep-wrap" data-screen-label="Exam Prep">
      <header className="exam-prep-header">
        <div>
          <h2>Exam Prep · {activeCourse}</h2>
          {bankView && (
            <div className="exam-prep-overall">
              <span><b>{bankView.total_mastered}</b> / {bankView.total_questions} questions mastered</span>
              <span className="dot">·</span>
              <span><b>{bankView.mastered_topics}</b> / {bankView.topic_count} topics fully mastered</span>
              {bankView.total_attempts > 0 && (
                <React.Fragment>
                  <span className="dot">·</span>
                  <span><b>{bankView.total_attempts}</b> attempts</span>
                  <span className="dot">·</span>
                  <span className="mono">{Math.round((bankView.overall_correct_rate || 0) * 100)}% correct</span>
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
                const ok = window.confirm(
                  "Re-extract exam topics? Existing questions are preserved for any topic whose name matches the new extraction (normalized). Topics whose names changed will have their questions moved to an archive bucket you can still see. Continue?"
                );
                if (ok) handlePlan(true);
              }}
              disabled={loading}
              title="Run topic extraction again. Mastery history for renamed topics moves to an archive."
            >
              Re-extract topics
            </button>
          )}
          {hasTopics && (
            <button className="btn ghost danger" onClick={handleReset} disabled={loading}>
              Reset bank
            </button>
          )}
        </div>
      </header>

      {error && <div className="error-banner">{error}</div>}

      {/* Empty state — no topics yet */}
      {!loading && !hasTopics && view === "topics" && (
        <div className="exam-prep-empty">
          <p>No exam bank yet for this course. Extract topics from the course materials to begin.</p>
          <button className="btn primary" onClick={() => handlePlan(false)} disabled={loading}>
            Extract Exam Topics
          </button>
        </div>
      )}

      {loading && (
        <div className="exam-prep-loading">
          <div className="exam-prep-loading-label">{loadingLabel || "Working…"}</div>
          <div className="exam-prep-loading-elapsed mono">{elapsedSec}s elapsed{elapsedSec >= 60 ? " — GPT-5.5 reasoning can take up to 120s before timing out" : ""}</div>
        </div>
      )}

      {/* Topics list */}
      {!loading && hasTopics && view === "topics" && (
        <ExamPrepTopics
          bankView={bankView}
          onStartMixed={() => startQuiz(null)}
          onStartTopic={(tid) => startQuiz(tid)}
        />
      )}

      {/* Active quiz */}
      {view === "quiz" && (
        <ExamPrepQuiz
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

function ExamPrepTopics({ bankView, onStartMixed, onStartTopic }) {
  const liveTopics = (bankView.topics || []).filter(t => !t.is_archived);
  const archivedTopics = (bankView.topics || []).filter(t => t.is_archived);
  return (
    <div className="exam-prep-topics">
      <div className="exam-prep-cta-row">
        <button className="btn primary" onClick={onStartMixed}>
          Start Mixed Quiz · all non-mastered topics
        </button>
      </div>
      <div className="topic-grid">
        {liveTopics.map(t => {
          const pct = Math.round((t.mastery_ratio || 0) * 100);
          return (
            <div key={t.id} className={"topic-card" + (t.is_mastered ? " mastered" : "")}>
              <div className="topic-card-head">
                <h3>{t.name}</h3>
                {t.is_mastered && <span className="topic-mastered-chip">✓ mastered</span>}
              </div>
              <div className="topic-meta">
                <span className="mono">weight · {Math.round((t.weight || 0) * 100)}%</span>
                <span className="dot">·</span>
                <span>{t.mastered_count} / {t.question_count} mastered</span>
              </div>
              {t.attempt_count > 0 && (
                <div className="topic-meta topic-attempts">
                  <span>{t.attempt_count} attempt{t.attempt_count === 1 ? "" : "s"}</span>
                  <span className="dot">·</span>
                  <span className={"topic-correct-rate " + (t.correct_rate >= 0.7 ? "good" : t.correct_rate >= 0.4 ? "mid" : "bad")}>
                    {Math.round((t.correct_rate || 0) * 100)}% correct
                  </span>
                </div>
              )}
              <div className="topic-progress">
                <div className="topic-progress-bar" style={{ width: pct + "%" }} />
              </div>
              <button
                className="btn ghost topic-quiz-btn"
                onClick={() => onStartTopic(t.id)}
                title={t.is_mastered ? "Re-quiz this topic (already mastered)" : `Start quiz on ${t.name}`}
              >
                {t.is_mastered ? "Review mastered →" : "Quiz on this topic →"}
              </button>
            </div>
          );
        })}
      </div>
      {archivedTopics.length > 0 && (
        <div className="topic-archive-note">
          <strong>Archive</strong> · {archivedTopics.length} bucket(s) of orphan questions from previous re-extracts (not sampled for new quizzes).
        </div>
      )}
    </div>
  );
}

function ExamPrepQuiz({ questions, scope, answers, onAnswer, onSubmit, onCancel, loading }) {
  const answered = Object.keys(answers).filter(k => answers[k] != null && answers[k] !== "").length;
  return (
    <div className="exam-prep-quiz">
      <div className="exam-prep-quiz-head">
        <div>
          <strong>Quiz · {questions.length} questions</strong>
          {scope && <span className="mono"> · scoped to topic</span>}
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
              placeholder="Your answer…"
              value={answers[q.id] || ""}
              onChange={e => onAnswer(q.id, e.target.value)}
            />
          )}
        </div>
      ))}

      <div className="exam-prep-quiz-footer">
        <button className="btn ghost" onClick={onCancel} disabled={loading}>Back</button>
        <button className="btn primary" onClick={onSubmit} disabled={loading || answered === 0}>
          Submit · grade {answered} answer{answered === 1 ? "" : "s"}
        </button>
      </div>
    </div>
  );
}

function ExamPrepResult({ graded, questions, answers, onContinue, onAgain }) {
  const total = graded.graded.length;
  const right = graded.graded.filter(g => g.correct).length;
  const wrong = total - right;
  const variants = graded.variants_added || {};
  const variantCount = Object.values(variants).reduce((s, n) => s + n, 0);
  const qById = {};
  questions.forEach(q => { qById[q.id] = q; });

  return (
    <div className="exam-prep-result">
      <div className="exam-result-summary">
        <div className="exam-result-stat correct">
          <b>{right}</b>
          <span>correct</span>
        </div>
        <div className="exam-result-stat wrong">
          <b>{wrong}</b>
          <span>wrong</span>
        </div>
        <div className="exam-result-stat">
          <b>{Math.round(total ? right * 100 / total : 0)}%</b>
          <span>score</span>
        </div>
        {variantCount > 0 && (
          <div className="exam-result-stat variants" title={`Self-evolution: ${graded.variant_budget_per_topic} variants per wrong topic`}>
            <b>+{variantCount}</b>
            <span>fresh variants generated</span>
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
                <div><strong>Your answer:</strong> {userAns || <em>(empty)</em>}</div>
                {!g.correct && (
                  <div><strong>Expected:</strong> {g.expected}</div>
                )}
                {g.explanation && (
                  <div className="dim"><strong>Why:</strong> {g.explanation}</div>
                )}
              </div>
            </div>
          );
        })}
      </div>

      <div className="exam-prep-quiz-footer">
        <button className="btn ghost" onClick={onContinue}>Back to Topics</button>
        <button className="btn primary" onClick={onAgain}>Another Round</button>
      </div>
    </div>
  );
}

Object.assign(window, { ExamPrep });
