/* global React, READER_DOC */
const { useState: useStateR } = React;

function ReaderParagraph({ p, highlightedId, onHighlight, onCite }) {
  if (p.kind === "h2") {
    return <h2><span className="num mono">§ {p.num}</span>{p.text}</h2>;
  }
  if (p.kind === "figure") {
    return (
      <div className="figure">
        <div className="fig-body">[ {p.body} ]</div>
        <div className="fig-cap"><b>FIG. {p.num}</b>{p.caption}</div>
      </div>
    );
  }
  // paragraph: may have cites array interleaved as children
  return (
    <p>
      {p.text}
      {p.cites && p.cites.map((c, i) => {
        if (typeof c === "string") return <span key={i}>{c}</span>;
        const isHot = highlightedId === c.id;
        return (
          <span key={i}>
            <span
              className={"hl" + (isHot ? " active" : "")}
              onMouseEnter={() => onHighlight(c.id)}
              onMouseLeave={() => onHighlight(null)}
              onClick={() => onCite(c.id)}
            >{c.text}</span>
          </span>
        );
      })}
      {p.cites && <span className="cite" onClick={() => onCite(p.cites.find(c => typeof c !== "string")?.id)}>1</span>}
    </p>
  );
}

function Reader({ onHighlight, highlightedId, onCite }) {
  return (
    <div className="reader" data-screen-label="Reader">
      <article className="page">
        <div className="chapter-eye mono">{READER_DOC.chapter}</div>
        <h1>{READER_DOC.title}</h1>
        <div className="sub serif">{READER_DOC.sub}</div>

        {READER_DOC.body.map((p, i) => (
          <ReaderParagraph
            key={i}
            p={p}
            highlightedId={highlightedId}
            onHighlight={onHighlight}
            onCite={onCite}
          />
        ))}

        <div className="ornament">· · ·</div>

        <div className="page-footer mono">
          <span>Marginalia · Organic Chemistry 301</span>
          <span>Page 142 of 280</span>
        </div>
      </article>
    </div>
  );
}

Object.assign(window, { Reader });
