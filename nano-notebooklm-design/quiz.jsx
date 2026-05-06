/* global React, QUIZ_DATA */
const { useState: useStateQ } = React;

function Quiz() {
  const [answers, setAnswers] = useStateQ({}); // { qIdx: letter }
  const [submitted, setSubmitted] = useStateQ(false);

  const answered = Object.keys(answers).length;
  const pct = Math.round((answered / QUIZ_DATA.questions.length) * 100);

  return (
    <div className="quiz-wrap" data-screen-label="Quiz">
      <div className="quiz">
        <div className="quiz-header">
          <h1 className="serif">{QUIZ_DATA.title}</h1>
          <div className="points mono">Total · 10 pts</div>
        </div>
        <div className="quiz-meta">
          {QUIZ_DATA.meta.map((m, i) => (
            <span key={i}>{m.k}<b>{m.v}</b></span>
          ))}
        </div>

        <div className="q-progress">
          <div className="lbl">Progress</div>
          <div className="bar"><div style={{ width: pct + "%" }}></div></div>
          <div className="lbl">{answered} / {QUIZ_DATA.questions.length}</div>
        </div>

        {QUIZ_DATA.questions.map((q, qi) => (
          <div className="question" key={qi}>
            <div className="q-num">
              <span>Q. {String(qi + 1).padStart(2, "0")}</span>
              <span className="type">{q.type}</span>
              <span className="pts">{q.pts} pts</span>
            </div>
            <div className="q-prompt serif">{q.prompt}</div>

            {q.options ? (
              <div className="q-options">
                {q.options.map(o => {
                  const picked = answers[qi] === o.l;
                  let cls = "q-opt";
                  if (submitted && q.correct) {
                    if (o.l === q.correct) cls += " correct";
                    else if (picked) cls += " wrong";
                  } else if (picked) {
                    cls += " selected";
                  }
                  return (
                    <div
                      key={o.l}
                      className={cls}
                      onClick={() => !submitted && setAnswers(a => ({ ...a, [qi]: o.l }))}
                    >
                      <span className="letter">{o.l}</span>
                      <span>{o.t}</span>
                      {submitted && q.correct && o.l === q.correct && <span className="tick">correct</span>}
                      {submitted && q.correct && picked && o.l !== q.correct && <span className="tick">your answer</span>}
                    </div>
                  );
                })}
                {submitted && q.explain && (
                  <div className="q-explain">
                    <div className="lbl">Explanation</div>
                    {q.explain}
                  </div>
                )}
              </div>
            ) : (
              <textarea className="q-essay" placeholder="Write your answer…" onChange={e => setAnswers(a => ({ ...a, [qi]: e.target.value }))}></textarea>
            )}
          </div>
        ))}

        <div className="quiz-footer">
          <button className="btn ghost" onClick={() => { setAnswers({}); setSubmitted(false); }}>Reset</button>
          <div style={{ display: "flex", gap: 8 }}>
            <button className="btn ghost">Save draft</button>
            <button className="btn primary" onClick={() => setSubmitted(true)}>{submitted ? "Review answers" : "Grade with AI"}</button>
          </div>
        </div>
      </div>
    </div>
  );
}

Object.assign(window, { Quiz });
