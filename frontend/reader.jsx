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

function Reader({ sources, activeId, activePage, onHighlight, highlightedId, onCite, notice }) {
  const source = (sources || []).find(s => s.id === activeId) || (sources || [])[0];
  const pageLabel = activePage ? `Page ${activePage}` : "Overview";
  return (
    <div className="reader" data-screen-label="Reader">
      <article className="page">
        {notice && <div className="reader-notice">{notice}</div>}
        <div className="chapter-eye mono">{READER_DOC.chapter}</div>
        <h1>{source ? source.title : READER_DOC.title}</h1>
        <div className="sub serif">{source ? `${pageLabel} · ${source.meta || ""}` : READER_DOC.sub}</div>

        {highlightedId && (
          <div className="reader-target active">
            Highlighted chunk <b>{highlightedId}</b>
          </div>
        )}

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
          <span>nano-NOTEBOOKLM Reader</span>
          <span>{pageLabel}</span>
        </div>
      </article>
    </div>
  );
}

Object.assign(window, { Reader });
