/* global React, NOTES_DATA */

function NotesOutline({ streaming, progress }) {
  // progress = number of sections revealed (0..3). If streaming, last one has cursor.
  const shown = streaming ? progress : NOTES_DATA.outline.length;
  return (
    <div className="outline">
      {NOTES_DATA.outline.slice(0, shown).map((s, i) => {
        const isLast = streaming && i === shown - 1;
        return (
          <div className="n-section" key={i}>
            <div className="n-h serif">
              <span className="roman">{s.roman}</span>
              {s.h}
            </div>
            <p className="n-p">{s.p}{isLast && <span className="stream-cursor"></span>}</p>
            {!isLast && s.subs.map((sub, j) => (
              <div className="n-sub" key={j}><b>{sub.b}</b>{" "}{sub.t}</div>
            ))}
            {!isLast && s.callout && (
              <div className="n-callout">{s.callout}</div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function NotesCornell() {
  return (
    <div className="cornell">
      <div className="cues">
        <div className="cue">What defines syn vs anti addition?</div>
        <div className="cue">Why no carbocation in Br₂ addition?</div>
        <div className="cue">Regiochemistry of hydroboration?</div>
        <div className="cue">Evidence for the bromonium ion?</div>
      </div>
      <div className="body-c">
        <h3>§ 7.3 Stereochemistry of Addition</h3>
        <p>Additions fall into two classes: <b>syn</b>, where both new bonds form on the same π-face, and <b>anti</b>, where they form on opposite faces. The classification predicts the diastereomer obtained.</p>
        <p>Br₂ addition proceeds through a three-membered <b>bromonium ion</b>, not a free carbocation. Nucleophilic Br⁻ attacks the back face, producing the anti-dihalide exclusively. Stereospecificity — (E)-alkene to (R,R)/(S,S), (Z)-alkene to meso — confirms the bridged intermediate.</p>
        <p><b>Hydroboration</b> is the canonical syn case. The four-centred transition state delivers H and B to the same face in one step; anti-Markovnikov regiochemistry comes from steric placement of the larger boron at the less-substituted carbon.</p>
      </div>
      <div className="summary"><b>Summary</b>Two modes — anti (halonium) and syn (concerted) — unify the chapter. Mechanistic evidence: stereospecific products and first-order kinetics in both reactants.</div>
    </div>
  );
}

function NotesCards() {
  const cards = [
    { tag: "Definition · §7.3", title: "Syn vs anti addition", body: "Syn = both new bonds to the same π-face. Anti = opposite faces. Determines the diastereomer, not the regiochemistry.", ref: "→ Clayden p. 432" },
    { tag: "Mechanism · §7.3.1", title: "Bromonium-ion intermediate", body: "Three-membered cyclic ion formed by Br⁺ bridging the π-bond. Nucleophile attacks the back face; product is anti-dihalide.", ref: "→ Roberts & Kimball, 1937" },
    { tag: "Mechanism · §7.3.2", title: "Hydroboration TS", body: "Four-centred concerted transition state. B–H and C–C form simultaneously on the same face → syn.", ref: "→ Lect. 12, slide 23" },
    { tag: "Evidence", title: "Why not a free cation?", body: "If Br⁺ generated a carbocation, 1,2-shifts would scramble stereochemistry. They don't — so the ion must be bridged.", ref: "→ Problem 5·Q3" },
    { tag: "Regiochemistry", title: "Markovnikov vs anti-Markov.", body: "Br₂: symmetric addition, Markovnikov irrelevant. HBr: Markov. HBO: anti-Markov, OH on less-substituted C.", ref: "→ §7.3.2" },
    { tag: "Exam flag", title: "Common misstep", body: "Students confuse the syn/anti face designation with cis/trans of the alkene. They are independent axes.", ref: "→ March '24 midterm Q2" },
  ];
  return (
    <div className="cards">
      {cards.map((c, i) => (
        <div className="note-card" key={i}>
          <div className="tag mono">{c.tag}</div>
          <h4 className="serif">{c.title}</h4>
          <p>{c.body}</p>
          <div className="ref mono">{c.ref}</div>
        </div>
      ))}
    </div>
  );
}

function Notes({ style, streaming, streamProgress }) {
  return (
    <div className="notes-wrap" data-screen-label="Notes">
      <div className="notes">
        <div className="notes-header">
          <div>
            <h1 className="serif">{NOTES_DATA.title}</h1>
          </div>
          <div className="meta mono">{NOTES_DATA.generated}</div>
        </div>

        {style === "outline" && <NotesOutline streaming={streaming} progress={streamProgress} />}
        {style === "cornell" && <NotesCornell />}
        {style === "cards" && <NotesCards />}
      </div>
    </div>
  );
}

Object.assign(window, { Notes });
