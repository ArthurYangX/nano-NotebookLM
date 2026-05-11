/**
 * latex-to-html.js — minimal LaTeX subset → HTML for the Note preview pane.
 *
 * The Note pipeline ships pure LaTeX from the backend (/api/notes and
 * /api/notes/stream). The frontend has to render that LaTeX without
 * shipping a full TeX engine. We do NOT support arbitrary LaTeX — we cover
 * exactly the macro / environment whitelist that NOTE_FORMAT_LATEX permits
 * in the backend prompt (nano_notebooklm/ai/prompt_templates.py).
 *
 * Anything outside the whitelist falls into a styled <pre class="latex-unknown">
 * tag so nothing silently disappears — students can see (and complain about)
 * a hallucinated macro instead of wondering where their content went.
 *
 * Pipeline:
 *   1. Stash math ($...$, \(...\), $$...$$, \[...\]) via NanoMarkdown.stashMath
 *      — KaTeX auto-render sweeps the final DOM, this just protects from
 *      regex mangling here.
 *   2. Stash \cite{file:loc} → CITE_n placeholders.
 *   3. Stash full \begin{env}...\end{env} blocks → ENV_n placeholders (so
 *      regex passes don't reach inside them). Multi-pass to handle nesting.
 *   4. Stash unknown envs as UNK_n placeholders → <pre> fallback later.
 *   5. HTML-escape the remaining prose (defense in depth: even if the LLM
 *      smuggles a <script> through, it lands as text).
 *   6. Map structural macros (\section, \subsection, \textbf, \emph).
 *   7. Wrap paragraphs.
 *   8. Restore env / cite / math placeholders.
 *
 * Output is a string ready for dangerouslySetInnerHTML. Caller is
 * responsible for calling NanoMarkdown.renderMath(node) after mount.
 */
(function () {
  var ENV_NAMES = ["definition", "theorem", "lemma", "example",
                   "remark", "proof", "itemize", "enumerate",
                   "equation", "align", "align*"];

  function escapeHtml(s) {
    if (typeof NanoMarkdown !== "undefined" && NanoMarkdown.escapeHtml) {
      return NanoMarkdown.escapeHtml(s);
    }
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // Match { ... } with one level of brace nesting. Sufficient for the
  // whitelisted macros (no recursive command arguments are part of the spec).
  var BRACE_GROUP = "\\{((?:[^{}]|\\{[^{}]*\\})*)\\}";

  function renderInline(text) {
    return text
      .replace(new RegExp("\\\\textbf\\s*" + BRACE_GROUP, "g"),
               function (_m, inner) { return "<strong>" + inner + "</strong>"; })
      .replace(new RegExp("\\\\emph\\s*" + BRACE_GROUP, "g"),
               function (_m, inner) { return "<em>" + inner + "</em>"; })
      .replace(new RegExp("\\\\texttt\\s*" + BRACE_GROUP, "g"),
               function (_m, inner) { return "<code>" + inner + "</code>"; })
      .replace(new RegExp("\\\\textit\\s*" + BRACE_GROUP, "g"),
               function (_m, inner) { return "<em>" + inner + "</em>"; });
  }

  function renderHeadings(text) {
    return text
      .replace(new RegExp("\\\\section\\s*" + BRACE_GROUP, "g"),
               function (_m, title) {
                 var id = slugify(title);
                 return "<h2 id=\"" + escapeHtml(id) + "\" data-toc-id=\"" + escapeHtml(id) + "\">" + title + "</h2>";
               })
      .replace(new RegExp("\\\\subsection\\s*" + BRACE_GROUP, "g"),
               function (_m, title) {
                 var id = slugify(title);
                 return "<h3 id=\"" + escapeHtml(id) + "\" data-toc-id=\"" + escapeHtml(id) + "\" style=\"margin-top:16px\">" + title + "</h3>";
               })
      .replace(new RegExp("\\\\subsubsection\\s*" + BRACE_GROUP, "g"),
               function (_m, title) { return "<h4>" + title + "</h4>"; });
  }

  function slugify(s) {
    return String(s).toLowerCase()
      .replace(/<[^>]+>/g, "")
      .replace(/[^\p{L}\p{N}\s-]/gu, "")
      .trim()
      .replace(/\s+/g, "-")
      .slice(0, 80) || "section";
  }

  function envClassFor(env) {
    if (env === "theorem")    return "thm-box thm-theorem";
    if (env === "lemma")      return "thm-box thm-lemma";
    if (env === "definition") return "thm-box thm-definition";
    if (env === "example")    return "thm-box thm-example";
    if (env === "remark")     return "thm-box thm-remark";
    if (env === "proof")      return "thm-box thm-proof";
    return "";
  }

  function envLabelFor(env) {
    if (env === "theorem")    return "Theorem";
    if (env === "lemma")      return "Lemma";
    if (env === "definition") return "Definition";
    if (env === "example")    return "Example";
    if (env === "remark")     return "Remark";
    if (env === "proof")      return "Proof";
    return "";
  }

  function renderThmFamily(env, optionalName, renderedInner) {
    var klass = envClassFor(env);
    if (!klass) return null;
    var label = envLabelFor(env);
    // XSS-hardening (review-swarm fix-all v1 #1b): optionalName comes from
    // `\begin{theorem}[<...>]` PRE-escape; it's stashed in envBuf before the
    // Stage-5 HTML escape ever runs, so without explicit escape an attacker
    // (LLM or user) could ship `\begin{theorem}[<img src=x onerror=...>]`
    // and the inner HTML would land verbatim in the DOM.
    var safeName = optionalName ? escapeHtml(optionalName) : "";
    var head = label
      ? "<b class=\"thm-label\">" + label + (safeName ? " (" + safeName + ")" : "") + ".</b> "
      : "";
    var tail = (env === "proof") ? " <span class=\"qed\">&#9633;</span>" : "";
    return "<div class=\"" + klass + "\">" + head + renderedInner + tail + "</div>";
  }

  function latexToHtml(source) {
    if (!source) return "";

    // Stage 1: stash math placeholders.
    var mathStash = (typeof NanoMarkdown !== "undefined" && NanoMarkdown.stashMath)
      ? NanoMarkdown.stashMath(source)
      : { text: String(source), restore: function (h) { return h; } };
    var text = mathStash.text;

    // Stage 2: stash \cite{...}.
    var citeBuf = [];
    text = text.replace(new RegExp("\\\\cite\\s*" + BRACE_GROUP, "g"),
      function (_m, inner) {
        citeBuf.push(inner);
        return " CITE" + (citeBuf.length - 1) + " ";
      });

    // Stage 3: stash whitelisted envs. Multi-pass for nesting (cap 6).
    var envBuf = [];
    var envAlternation = ENV_NAMES.map(function (e) { return e.replace("*", "\\*"); }).join("|");
    var envRe = new RegExp(
      "\\\\begin\\{(" + envAlternation + ")\\}" +
      "(?:\\[([^\\]\\n]*)\\])?" +
      "([\\s\\S]*?)" +
      "\\\\end\\{\\1\\}",
      "g"
    );
    for (var pass = 0; pass < 6; pass++) {
      var touched = false;
      text = text.replace(envRe, function (_m, env, optionalName, inner) {
        touched = true;
        envBuf.push({ env: env, optionalName: optionalName || "", inner: inner });
        return " ENV" + (envBuf.length - 1) + " ";
      });
      if (!touched) break;
    }

    // Stage 4: stash unknown envs (escape hatch — not on whitelist).
    var unknownBuf = [];
    text = text.replace(
      /\\begin\{([a-zA-Z*]+)\}([\s\S]*?)\\end\{\1\}/g,
      function (m) {
        unknownBuf.push(m);
        return " UNK" + (unknownBuf.length - 1) + " ";
      }
    );

    // Stage 5: HTML-escape the remaining prose.
    var html = escapeHtml(text);

    // Stage 6: structural macros. Headings + inline.
    html = renderHeadings(html);
    html = renderInline(html);

    // Stray \item outside a list — render as bullet.
    html = html.replace(/\\item\b\s*/g, "&bull; ");

    // Paragraph wrap.
    html = html.split(/\n\s*\n/)
      .map(function (p) { return p.trim(); })
      .filter(Boolean)
      .map(function (p) { return "<p>" + p.replace(/\n/g, " ") + "</p>"; })
      .join("\n");

    // Stage 7: restore unknown envs as escaped <pre> blocks.
    // The placeholder may have lost its surrounding whitespace after the
    // paragraph trim; allow optional whitespace either side.
    html = html.replace(/\s?UNK(\d+)\s?/g, function (_m, idx) {
      var raw = unknownBuf[Number(idx)];
      return "<pre class=\"latex-unknown\">" + escapeHtml(raw) + "</pre>";
    });

    // Stage 8: restore whitelisted envs. List envs (itemize/enumerate)
    // split on the RAW \item. Math envs emit $$...$$ for KaTeX. Theorem-
    // family envs wrap the inner with the appropriate box class.
    //
    // LaTeX-output fix-all v3 #4: renderInnerFragment used to recursively
    // call latexToHtml(inner). That spawned a fresh local citeBuf in the
    // recursion, which eagerly consumed CITE_n / MATH_n placeholders left
    // by the OUTER stash pass — looking them up in the (empty) recursive
    // buffer and emitting empty chips (`[Source: ]`). Symptom: every cite
    // inside a theorem/definition env jumped to the first slide because
    // resolveCitationNavigation matched "" via title.includes(""). Fix:
    // the inner fragment is rendered INLINE only (escape + inline macros),
    // preserving every placeholder verbatim so the outer Stage 9 / 10 / 11
    // sweep restores them correctly.
    function renderInnerFragment(inner) {
      var escaped = escapeHtml(inner);
      escaped = renderInline(escaped);
      // Blank lines → soft break inside the box; single newlines collapse.
      escaped = escaped.replace(/\n\s*\n/g, "<br/><br/>").replace(/\n/g, " ");
      return escaped;
    }
    function renderListInner(rawInner) {
      return rawInner.split(/\\item\b\s*/)
        .map(function (s) { return s.trim(); })
        .filter(Boolean)
        .map(function (s) { return "<li>" + renderInnerFragment(s) + "</li>"; })
        .join("");
    }
    html = html.replace(/\s?ENV(\d+)\s?/g, function (_m, idx) {
      var spec = envBuf[Number(idx)];
      var env = spec.env;
      var optionalName = spec.optionalName;
      var inner = spec.inner;
      if (env === "itemize") return "<ul>" + renderListInner(inner) + "</ul>";
      if (env === "enumerate") return "<ol>" + renderListInner(inner) + "</ol>";
      if (env === "equation" || env === "align" || env === "align*") {
        // XSS-hardening (review-swarm fix-all v1 #1a): equation / align inner
        // bodies were stashed PRE-escape, so concatenating them raw into
        // dangerouslySetInnerHTML let `\begin{equation}</div><script>...\end`
        // execute in the app origin. HTML-escape first; KaTeX auto-render
        // reads textContent (post-HTML-parse) so `&lt;` round-trips back to
        // `<` for legitimate math like `$a<b$` — no rendering regression.
        return "<div class=\"math-display\">$$" + escapeHtml(inner.trim()) + "$$</div>";
      }
      var rendered = renderThmFamily(env, optionalName, renderInnerFragment(inner));
      if (rendered === null) {
        return "<pre class=\"latex-unknown\">" + escapeHtml(inner) + "</pre>";
      }
      return rendered;
    });

    // Stage 9: restore cite chips.
    //
    // LaTeX-output fix-all v3 #5: normalise the cite payload from
    // `file:loc` (the format the LLM emits) into `file, loc` (the format
    // resolveCitationNavigation in study-state.js parses — it splits on
    // `,` to extract the source filename from the location). Without the
    // normalisation, sourcePart was the full `file:loc` string and the
    // matcher fell back to a substring `title.includes(sourcePart)` /
    // `sourcePart.includes(title)` check, which still works but is fragile
    // and loses the chunk_id route. Comma-form is the canonical contract.
    function normaliseCite(raw) {
      var i = raw.indexOf(":");
      if (i <= 0 || i >= raw.length - 1) return raw;  // no usable split
      // First-colon split — location text may itself contain colons (e.g.
      // "Section 2.1: Intro" is rare but defensible) so don't be greedy.
      var file = raw.slice(0, i).trim();
      var loc = raw.slice(i + 1).trim();
      if (!file || !loc) return raw;
      return file + ", " + loc;
    }
    html = html.replace(/\s?CITE(\d+)\s?/g, function (_m, idx) {
      var rawInner = citeBuf[Number(idx)] || "";
      var displayInner = normaliseCite(rawInner);
      var safeInner = escapeHtml(displayInner);
      var safeFull  = escapeHtml("[Source: " + displayInner + "]");
      return "<button type=\"button\" class=\"ref-chip mono\" data-cite=\"" + safeFull + "\">" + safeInner + "</button>";
    });

    // Stage 10: restore math placeholders.
    html = mathStash.restore(html);

    // Collapse empty paragraphs that math-display blocks hoist out of.
    html = html.replace(/<p>\s*<\/p>/g, "");
    return html;
  }

  // Extract a TOC from raw LaTeX source. Used by the Notes panel to
  // populate its sidebar before the latex-to-html pass completes.
  // Returns items with `text` (not `title`) so the consumer matches the
  // legacy StudyState.extractHeadingTOC shape — NotesTOC reads `it.text`.
  function extractTOC(source) {
    if (!source) return [];
    var items = [];
    var re = new RegExp("\\\\(section|subsection)\\s*" + BRACE_GROUP, "g");
    var m;
    while ((m = re.exec(source)) !== null) {
      // LaTeX-output fix-all v3 #3: strip inline macros from TOC titles —
      // the LLM happily emits `\subsection{\texttt{leaq}: ...}` and the
      // sidebar would otherwise show literal `\texttt{leaq}: ...`. Reduce
      // \texttt/\textbf/\emph/\textit/\code wrappers to their inner text.
      var clean = m[2]
        .replace(/\\(textbf|textit|emph|texttt|textsf|textrm)\s*\{([^{}]*)\}/g, "$2")
        .replace(/\\[a-zA-Z]+\s*\{([^{}]*)\}/g, "$1");
      items.push({
        level: m[1] === "section" ? 1 : 2,
        text: clean,
        id: slugify(clean),
      });
    }
    return items;
  }

  if (typeof window !== "undefined") {
    window.NanoLatex = {
      latexToHtml: latexToHtml,
      extractTOC: extractTOC,
      slugify: slugify,
    };
  }
  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      latexToHtml: latexToHtml,
      extractTOC: extractTOC,
      slugify: slugify,
    };
  }
})();
