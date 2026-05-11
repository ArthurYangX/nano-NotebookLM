/* global React, READER_DOC, API */
const { useState: useStateR, useEffect: useEffectR, useRef: useRefR, useMemo: useMemoR } = React;

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

// Citation viewer: existing prev/target/next behavior when the user clicks a
// chunk citation in the chat sidebar. Untouched from the pre-R5 Reader.
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

// Text-mode browse: render every chunk of a document in page order, with
// a sticky page marker so the reader keeps spatial context as they scroll.
function DocumentTextBody({ doc, activePage, highlightedId, onCite, navEpoch }) {
  // Group chunks by page so we can emit one page-anchor heading per page
  // — the highlight target for `activePage` and the click-to-cite handles.
  const groups = useMemoR(() => {
    const byPage = new Map();
    (doc.chunks || []).forEach(c => {
      const key = c.page == null ? "—" : String(c.page);
      if (!byPage.has(key)) byPage.set(key, []);
      byPage.get(key).push(c);
    });
    return Array.from(byPage.entries());
  }, [doc]);

  const targetRef = useRefR(null);
  useEffectR(() => {
    if (!activePage || !targetRef.current) return;
    targetRef.current.scrollIntoView({ behavior: "smooth", block: "start" });
    // navEpoch is in the deps so a repeat-click on the same citation
    // still re-fires scrollIntoView even though `activePage` is unchanged.
  }, [activePage, doc.doc_id, navEpoch]);

  return (
    <div className="doc-text-body">
      {groups.map(([page, chunks]) => {
        const isTarget = activePage && String(activePage) === page;
        return (
          <section
            key={page}
            className={"doc-page" + (isTarget ? " active" : "")}
            ref={isTarget ? targetRef : null}
            data-page={page}
          >
            <h3 className="doc-page-marker mono">
              {page === "—" ? "unpaged" : `Page ${page}`}
              <span className="doc-page-chunks">· {chunks.length} chunk{chunks.length === 1 ? "" : "s"}</span>
            </h3>
            {chunks.map(c => {
              const isHot = highlightedId === c.chunk_id;
              return (
                <p
                  key={c.chunk_id}
                  className={"doc-chunk" + (isHot ? " hot" : "")}
                  data-chunk-id={c.chunk_id}
                  onClick={() => onCite && onCite(c.chunk_id)}
                  title={c.section || c.location}
                >
                  {c.text}
                </p>
              );
            })}
          </section>
        );
      })}
    </div>
  );
}

// PDF mode: hand the file off to the browser's native PDF viewer via
// `<iframe>` + `#page=N` anchor. Avoids bundling pdf.js (~2MB) while still
// getting search, zoom, scroll, and native rendering for free.
function DocumentPdfFrame({ courseId, docId, sourceFile, activePage, navEpoch }) {
  const url = API.sourceFileUrl(courseId, docId, { page: activePage });
  const iframeRef = useRefR(null);
  // When the same citation is clicked twice in a row, `activePage` doesn't
  // change → React skips the iframe re-render → the PDF viewer never
  // re-jumps. We bump `navEpoch` on every dispatch and imperatively reassign
  // `src` here so the browser re-navigates to `#page=N`. Setting src to the
  // same value still triggers a fresh load in Chrome/Safari's PDFium
  // (verified against macOS 14 Chrome 130) — slight flicker but the
  // page-jump lands every time.
  useEffectR(() => {
    if (!iframeRef.current) return;
    iframeRef.current.src = url;
  }, [navEpoch, url]);
  return (
    <iframe
      ref={iframeRef}
      className="doc-pdf-frame"
      src={url}
      title={sourceFile}
      // No `sandbox`: Chrome's PDFium plugin renders inline PDFs as
      // plugin content, and any sandbox value suppresses it →
      // broken-doc icon instead of the PDF. Defense-in-depth is
      // preserved server-side via `X-Content-Type-Options: nosniff`
      // (see `api/server.py:get_source_file`). `referrerpolicy` still
      // strips the Referer header. Same posture as the in-Notes modal.
      referrerPolicy="no-referrer"
      // `loading="lazy"` is irrelevant here (only one iframe on page) but
      // setting allow=fullscreen lets users pop the PDF out in Chrome.
      allow="fullscreen"
    />
  );
}

function Reader({ sources, activeCourse, activeId, activePage, onHighlight, highlightedId, onCite, notice, navEpoch }) {
  const source = (sources || []).find(s => s.id === activeId) || (sources || [])[0];

  // Strip the `<sourceId>:<page>` synthetic ids that resolveCitationNavigation
  // emits for non-chunk citations — those have no backing chunk to fetch.
  const fetchableId = (highlightedId && !String(highlightedId).includes(":"))
    ? highlightedId : null;

  const [chunkData, setChunkData] = useStateR(null);
  const [chunkErr, setChunkErr] = useStateR(null);
  const [retryNonce, setRetryNonce] = useStateR(0);
  const targetRef = useRefR(null);
  const lastScrolledIdRef = useRefR(null);

  // Citation-detail fetch (chat citation click → /api/chunks/{chunk_id})
  useEffectR(() => {
    if (!fetchableId || typeof API === "undefined" || !API.getChunk) {
      setChunkData(null);
      setChunkErr(null);
      return;
    }
    const ac = (typeof AbortController !== "undefined") ? new AbortController() : null;
    setChunkErr(null);
    API.getChunk(fetchableId, { signal: ac ? ac.signal : undefined })
      .then(data => {
        if (ac && ac.signal.aborted) return;
        setChunkData(data);
      })
      .catch(err => {
        if (err && (err.name === "AbortError" || (ac && ac.signal.aborted))) return;
        setChunkData(null);
        setChunkErr(err && err.message ? err.message : "Failed to load chunk");
      });
    return () => { if (ac) ac.abort(); };
  }, [fetchableId, retryNonce]);

  // Document-browse fetch (library click → /api/source/.../{doc_id}/chunks).
  // Skipped when a citation is active (citation view takes priority).
  const [docData, setDocData] = useStateR(null);
  const [docErr, setDocErr] = useStateR(null);

  // Guard against the cross-course race: when the user switches activeCourse,
  // activeId may still point at a doc_id from the previous course for one
  // render (parent re-fetches sources, then resets activeId). Suppress the
  // fetch until `sources` confirms activeId belongs to the current course.
  const activeIdInSources = (sources || []).some(s => s.id === activeId);

  useEffectR(() => {
    if (!activeCourse || !activeId || fetchableId || !activeIdInSources) {
      setDocData(null);
      setDocErr(null);
      return;
    }
    if (typeof API === "undefined" || !API.getSourceChunks) return;
    const ac = (typeof AbortController !== "undefined") ? new AbortController() : null;
    setDocErr(null);
    API.getSourceChunks(activeCourse, activeId, { signal: ac ? ac.signal : undefined })
      .then(d => {
        if (ac && ac.signal.aborted) return;
        setDocData(d);
      })
      .catch(err => {
        if (err && (err.name === "AbortError" || (ac && ac.signal.aborted))) return;
        setDocData(null);
        setDocErr(err && err.message ? err.message : "Failed to load document");
      });
    return () => { if (ac) ac.abort(); };
  }, [activeCourse, activeId, fetchableId, activeIdInSources]);

  // Smooth-scroll citation target only when the chunk_id actually changes.
  useEffectR(() => {
    const cid = chunkData && chunkData.chunk && chunkData.chunk.chunk_id;
    if (!cid || !targetRef.current) return;
    if (lastScrolledIdRef.current === cid) return;
    lastScrolledIdRef.current = cid;
    targetRef.current.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [chunkData]);

  const showRealChunk = !!chunkData && !!chunkData.chunk;
  const showPdfFrame = !showRealChunk && docData && docData.file_type === "pdf" && docData.file_available;
  const showTextDoc = !showRealChunk && !showPdfFrame && docData && (docData.chunks || []).length > 0;
  const showIntro = !showRealChunk && !showPdfFrame && !showTextDoc;

  const pageLabel = activePage ? `Page ${activePage}` : "Overview";
  let banner;
  if (showRealChunk) banner = `《${chunkData.source_file}》 · Page ${chunkData.page ?? "—"}`;
  else if (docData) {
    const pages = docData.page_range;
    banner = pages
      ? `${docData.total_chunks} chunks · pages ${pages[0]}–${pages[1]}${docData.file_type ? " · " + docData.file_type : ""}`
      : `${docData.total_chunks} chunks${docData.file_type ? " · " + docData.file_type : ""}`;
  }
  else banner = source ? `${pageLabel} · ${source.meta || ""}` : READER_DOC.sub;

  const heading = showRealChunk ? chunkData.source_file
                 : docData ? docData.source_file
                 : source ? source.title : READER_DOC.title;
  const chapter = showRealChunk ? (chunkData.course_id || READER_DOC.chapter)
                 : docData ? (docData.course_id || activeCourse || READER_DOC.chapter)
                 : READER_DOC.chapter;

  return (
    <div className="reader" data-screen-label="Reader">
      <article className={"page" + (showPdfFrame ? " pdf-mode" : "")}>
        {notice && <div className="reader-notice">{notice}</div>}
        <div className="chapter-eye mono">{chapter}</div>
        <h1>{heading}</h1>
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

        {docErr && !docData && (
          <div className="reader-target">
            <span className="chunk-err">{docErr}</span>
          </div>
        )}

        {showRealChunk && <ChunkBlock data={chunkData} />}

        {showPdfFrame && (
          <DocumentPdfFrame
            courseId={activeCourse}
            docId={activeId}
            sourceFile={docData.source_file}
            activePage={activePage}
            navEpoch={navEpoch}
          />
        )}

        {showTextDoc && (
          <DocumentTextBody
            doc={docData}
            activePage={activePage}
            highlightedId={highlightedId}
            onCite={onCite}
            navEpoch={navEpoch}
          />
        )}

        {showIntro && READER_DOC.body.map((p, i) => (
          <ReaderParagraph
            key={i}
            p={p}
            highlightedId={highlightedId}
            onHighlight={onHighlight}
            onCite={onCite}
          />
        ))}

        {!showPdfFrame && <div className="ornament">· · ·</div>}

        <div className="page-footer mono">
          <span>nano-NOTEBOOKLM Reader</span>
          <span>{showRealChunk ? `《${chunkData.source_file}》` : pageLabel}</span>
        </div>
      </article>
    </div>
  );
}

Object.assign(window, { Reader, ChunkBlock, DocumentTextBody, DocumentPdfFrame });
