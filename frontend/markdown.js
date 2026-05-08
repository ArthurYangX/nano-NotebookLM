/**
 * Shared markdown / KaTeX helpers — used by chat bubbles (assistant.jsx),
 * the Notes preview (app.jsx), and any other surface that renders LLM output
 * through dangerouslySetInnerHTML.
 *
 * Why a shared layer: previously the chat path (renderMarkdown in
 * assistant.jsx) and the Notes path (markdownToHtml in app.jsx) had
 * independent regex pipelines and only the chat path ran KaTeX. Result:
 * paste a Notes view with `$T = O(n)$` and the user saw raw `$T = O(n)$` —
 * inconsistent with the chat side. This module exposes the math-rendering
 * piece (escape + KaTeX run) so every consumer can hand off post-mount.
 *
 * Plain JS (no JSX, no React) so it can be loaded via a regular <script>
 * tag before any Babel-transpiled component depends on it.
 */
(function () {
  // HTML-escape — chunk text and the wider rendered output may contain the
  // markdown-special chars `<` / `&` / `"` from PDFs (e.g. SGML, code samples).
  // Caller is expected to escape *before* applying markdown regexes, so the
  // escaped tags inside a <p> won't be mistaken for HTML.
  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  // Run KaTeX auto-render on `node`. KaTeX is loaded async via <script defer>;
  // poll briefly on first call. Bound retries so a CDN failure doesn't loop
  // forever. Subsequent calls (KaTeX present) are sync.
  function renderMath(node) {
    if (!node) return;
    var renderFn = (typeof window !== "undefined") && window.renderMathInElement;
    if (typeof renderFn !== "function") {
      var attempts = Number(node.dataset && node.dataset.mathRetry || 0);
      if (attempts < 8) {
        if (node.dataset) node.dataset.mathRetry = String(attempts + 1);
        setTimeout(function () { renderMath(node); }, 150);
      }
      return;
    }
    try {
      renderFn(node, {
        delimiters: [
          { left: "$$", right: "$$", display: true },
          { left: "$", right: "$", display: false },
          { left: "\\(", right: "\\)", display: false },
          { left: "\\[", right: "\\]", display: true },
        ],
        throwOnError: false,
        ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code"],
      });
    } catch (e) {
      if (typeof console !== "undefined") {
        console.warn("katex render failed:", e);
      }
    }
  }

  // Stash math expressions out of the source string so subsequent HTML-escape
  // / regex passes don't mangle the LaTeX. Returns { text, restore(html) }.
  // Caller flow:
  //   var s = NanoMarkdown.stashMath(raw);
  //   var html = doMarkdownRegex(escapeHtml(s.text));
  //   html = s.restore(html);   // splices $...$ back, with display math
  //                              // wrapped in <div class="math-display">
  function stashMath(raw) {
    var tokens = [];
    function stash(latex, displayMode) {
      var i = tokens.length;
      tokens.push({ latex: latex, displayMode: displayMode });
      return " MATH" + i + " ";
    }
    var text = String(raw || "")
      .replace(/\$\$([\s\S]+?)\$\$/g, function (_, m) { return stash(m, true); })
      .replace(/\\\[([\s\S]+?)\\\]/g, function (_, m) { return stash(m, true); })
      .replace(/\\\(([\s\S]+?)\\\)/g, function (_, m) { return stash(m, false); })
      .replace(/\$([^$\n]+?)\$/g, function (_, m) { return stash(m, false); });
    function restore(html) {
      return String(html).replace(/ MATH(\d+) /g, function (_, idx) {
        var m = tokens[Number(idx)];
        if (!m) return "";
        if (m.displayMode) {
          return '</p><div class="math-display">$$' + m.latex + '$$</div><p>';
        }
        return "$" + m.latex + "$";
      });
    }
    return { text: text, restore: restore };
  }

  // Throttle a function so rapid streaming-update callers (notes / report
  // partial chunks) don't re-render KaTeX 100×/s. Trailing-edge call
  // guarantees the final state is rendered.
  function throttle(fn, ms) {
    var last = 0;
    var pending = null;
    return function () {
      var args = arguments;
      var now = Date.now();
      var elapsed = now - last;
      var ctx = this;
      if (pending) { clearTimeout(pending); pending = null; }
      if (elapsed >= ms) {
        last = now;
        fn.apply(ctx, args);
      } else {
        pending = setTimeout(function () {
          last = Date.now();
          pending = null;
          fn.apply(ctx, args);
        }, ms - elapsed);
      }
    };
  }

  if (typeof window !== "undefined") {
    window.NanoMarkdown = {
      escapeHtml: escapeHtml,
      renderMath: renderMath,
      stashMath: stashMath,
      throttle: throttle,
    };
  }
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { escapeHtml: escapeHtml, renderMath: renderMath, stashMath: stashMath, throttle: throttle };
  }
})();
