/* global React, READER_DOC, API */
const { useState: useStateR, useEffect: useEffectR, useRef: useRefR } = React;

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

// Round 2.2 #R5: render a real chunk fetched from /api/chunks/{chunk_id} —
// the previous Reader always rendered hardcoded `READER_DOC` lorem-ipsum no
// matter which citation was clicked.
function ChunkBlock({ data }) {
  if (!data || !data.chunk) return null;
  return (
    <div className="chunk-block">
      {data.prev && (
        <p className="chunk-context">
          <span className="chunk-marker mono">prev · {data.prev.location}</span>
          {data.prev.text}
        </p>
      )}
      <p className="chunk-target">
        <span className="chunk-marker mono">chunk · {data.chunk.chunk_id}</span>
        {data.chunk.text}
      </p>
      {data.next && (
        <p className="chunk-context">
          <span className="chunk-marker mono">next · {data.next.location}</span>
          {data.next.text}
        </p>
      )}
    </div>
  );
}

function Reader({ sources, activeId, activePage, onHighlight, highlightedId, onCite, notice }) {
  const source = (sources || []).find(s => s.id === activeId) || (sources || [])[0];
  const pageLabel = activePage ? `Page ${activePage}` : "Overview";

  // Strip the `<sourceId>:<page>` synthetic ids that resolveCitationNavigation
  // emits for non-chunk citations — those have no backing chunk to fetch.
  const fetchableId = (highlightedId && !String(highlightedId).includes(":"))
    ? highlightedId : null;

  const [chunkData, setChunkData] = useStateR(null);
  const [chunkErr, setChunkErr] = useStateR(null);
  const [retryNonce, setRetryNonce] = useStateR(0);
  const targetRef = useRefR(null);
  // Track the chunk_id that scrollIntoView already fired for so we don't
  // re-trigger smooth-scroll on every chunkData object identity change
  // (fix-all v2 #4: review-swarm perf F6).
  const lastScrolledIdRef = useRefR(null);

  useEffectR(() => {
    if (!fetchableId || typeof API === "undefined" || !API.getChunk) {
      setChunkData(null);
      setChunkErr(null);
      return;
    }
    // fix-all v2 #2 (review-swarm perf F5): cancel stale fetches via
    // AbortController so rapid citation clicks don't pile up parallel work
    // on the backend (the chunks endpoint scans courses on cold cache).
    const ac = (typeof AbortController !== "undefined") ? new AbortController() : null;
    setChunkErr(null);
    API.getChunk(fetchableId, { signal: ac ? ac.signal : undefined })
      .then(data => {
        if (ac && ac.signal.aborted) return;
        setChunkData(data);
      })
      .catch(err => {
        // AbortError fires when the next click cancels us — silent drop.
        if (err && (err.name === "AbortError" || (ac && ac.signal.aborted))) return;
        setChunkData(null);
        setChunkErr(err && err.message ? err.message : "Failed to load chunk");
      });
    return () => { if (ac) ac.abort(); };
  }, [fetchableId, retryNonce]);

  // Smooth-scroll only when the *target chunk_id* changes (not on every
  // chunkData object identity update). `block: "nearest"` so already-visible
  // chunks don't jolt around.
  useEffectR(() => {
    const cid = chunkData && chunkData.chunk && chunkData.chunk.chunk_id;
    if (!cid || !targetRef.current) return;
    if (lastScrolledIdRef.current === cid) return;
    lastScrolledIdRef.current = cid;
    targetRef.current.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [chunkData]);

  const showRealChunk = !!chunkData && !!chunkData.chunk;
  const banner = showRealChunk
    ? `《${chunkData.source_file}》 · Page ${chunkData.page ?? "—"}`
    : (source ? `${pageLabel} · ${source.meta || ""}` : READER_DOC.sub);

  return (
    <div className="reader" data-screen-label="Reader">
      <article className="page">
        {notice && <div className="reader-notice">{notice}</div>}
        <div className="chapter-eye mono">
          {showRealChunk ? (chunkData.course_id || READER_DOC.chapter) : READER_DOC.chapter}
        </div>
        <h1>{showRealChunk ? chunkData.source_file : (source ? source.title : READER_DOC.title)}</h1>
        <div className="sub serif">{banner}</div>

        {highlightedId && (
          <div className="reader-target active" ref={targetRef}>
            Highlighted chunk <b>{highlightedId}</b>
            {chunkErr && (
              <span className="chunk-err">
                {" · "}{chunkErr}
                {" "}
                <button
                  className="chunk-retry mono"
                  onClick={() => setRetryNonce(n => n + 1)}
                >retry</button>
              </span>
            )}
          </div>
        )}

        {showRealChunk ? (
          <ChunkBlock data={chunkData} />
        ) : (
          READER_DOC.body.map((p, i) => (
            <ReaderParagraph
              key={i}
              p={p}
              highlightedId={highlightedId}
              onHighlight={onHighlight}
              onCite={onCite}
            />
          ))
        )}

        <div className="ornament">· · ·</div>

        <div className="page-footer mono">
          <span>nano-NOTEBOOKLM Reader</span>
          <span>{showRealChunk ? `《${chunkData.source_file}》` : pageLabel}</span>
        </div>
      </article>
    </div>
  );
}

Object.assign(window, { Reader, ChunkBlock });
