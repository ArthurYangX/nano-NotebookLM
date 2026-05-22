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

    // Stage 0: strip PDF-only cross-reference commands.
    //
    // LLMs trained on academic LaTeX often emit `\label{eq:foo}` after a
    // display equation and `\ref{eq:foo}` / `\eqref{eq:foo}` to cite it
    // back. These are tectonic-time numbering commands — KaTeX does not
    // implement any of them and raises "Undefined control sequence \label"
    // / `\ref` / `\eqref` (red error tooltips on the rendered page).
    //
    // Strategy: drop them silently from browser preview. The PDF path
    // (tectonic) sees the raw .tex with labels intact, so cross-refs
    // still work in the exported PDF. The brace pattern `\{[^}]*\}` is
    // intentionally simple — nested-brace labels like `\label{eq:f_{1}}`
    // are vanishingly rare; revisit if a real example surfaces.
    source = String(source)
      .replace(/\\label\s*\{[^}]*\}/g, "")
      .replace(/\\eqref\s*\{[^}]*\}/g, "")
      .replace(/\\ref\s*\{[^}]*\}/g, "");

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

    // Stage 3: stash whitelisted envs.
    //
    // Recursive descent (review-swarm fix-all): a regex like
    // `\\begin\{(env)\}[\s\S]*?\\end\{\1\}` with non-greedy `*?` + backref
    // only ever matches the OUTERMOST same-name end. A flat multi-pass loop
    // therefore stashes only top-level envs — any env nested inside
    // another's `inner` string survives into envBuf untouched. Stage 8
    // restores envs via `renderInnerFragment(inner)`, which is inline-only
    // (escape + inline macros) so a nested `\begin{proof}…\end{proof}`
    // would render as literal escaped text instead of a `.thm-proof` box.
    //
    // Fix: descend into each match's `inner` first, then push. Recursion
    // pushes into the SAME outer envBuf so ENV indices stay consistent
    // across nesting depths.
    var envBuf = [];
    var envAlternation = ENV_NAMES.map(function (e) { return e.replace("*", "\\*"); }).join("|");
    var envRe = new RegExp(
      "\\\\begin\\{(" + envAlternation + ")\\}" +
      "(?:\\[([^\\]\\n]*)\\])?" +
      "([\\s\\S]*?)" +
      "\\\\end\\{\\1\\}",
      "g"
    );
    function envStashRecursive(input) {
      return input.replace(envRe, function (_m, env, optionalName, inner) {
        var stashedInner = envStashRecursive(inner);
        envBuf.push({ env: env, optionalName: optionalName || "", inner: stashedInner });
        return " ENV" + (envBuf.length - 1) + " ";
      });
    }
    text = envStashRecursive(text);

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
    // Env restore: loop until no ENV_n placeholders remain. The recursive
    // envStashRecursive (Stage 3) preserved nested-env placeholders inside
    // outer entries' `inner` strings — renderInnerFragment doesn't expand
    // ENV_n by itself (it's inline-only), so a single replace pass would
    // leave `ENV0` literal text inside a `.thm-theorem` box that wraps a
    // nested proof. Loop with a cap (matches Stage 3 envBuf depth ceiling
    // implicitly via envBuf.length); break early when no placeholder
    // matched in a pass.
    function envRestore(_m, idx) {
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
        //
        // align bug fix (2026-05-22): `align` / `align*` bodies use `&` as
        // the alignment-column tab character, which is invalid inside a
        // bare `$$...$$` display block — KaTeX raises "Misplaced alignment
        // tab character &". Wrap the body in `\begin{aligned}...\end{aligned}`
        // (the math-mode equivalent of `align`) so the alignment columns
        // survive the trip through display math. `equation` has no `&`, so
        // it stays as-is.
        var body = escapeHtml(inner.trim());
        if (env === "align" || env === "align*") {
          body = "\\begin{aligned}" + body + "\\end{aligned}";
        }
        return "<div class=\"math-display\">$$" + body + "$$</div>";
      }
      var rendered = renderThmFamily(env, optionalName, renderInnerFragment(inner));
      if (rendered === null) {
        return "<pre class=\"latex-unknown\">" + escapeHtml(inner) + "</pre>";
      }
      return rendered;
    }
    var envPlaceholderRe = /\s?ENV(\d+)\s?/g;
    for (var envPass = 0; envPass < envBuf.length + 1; envPass++) {
      if (!envPlaceholderRe.test(html)) break;
      envPlaceholderRe.lastIndex = 0;
      html = html.replace(envPlaceholderRe, envRestore);
    }

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

  // Filename heuristic — full-course Note generation wraps each source
  // file in `\section{<filename>}` (concat_draft + _escape_latex_title on
  // the Python side). For the hierarchical TOC, any \section{} whose title
  // looks like a real upload (ends in a supported file extension) becomes
  // an L1 "file" node; everything else nests inside the most recent file.
  // Catches both raw (`lecture3.pdf`) and underscore-escaped (`lecture\_3.pdf`)
  // forms that the LaTeX-special escape pass emits.
  var FILE_EXT_RE = /\.(pdf|pptx?|docx?|md|markdown|txt)$/i;

  function looksLikeFilename(rawTitle) {
    if (!rawTitle) return false;
    // Drop the underscore-escape that _escape_latex_title injects so the
    // extension test sees the actual filename.
    var probe = String(rawTitle).replace(/\\([_&%$#{}])/g, "$1").trim();
    return FILE_EXT_RE.test(probe);
  }

  function cleanHeadingTitle(raw) {
    // LaTeX-output fix-all v3 #3: strip inline macros from TOC titles —
    // the LLM happily emits `\subsection{\texttt{leaq}: ...}` and the
    // sidebar would otherwise show literal `\texttt{leaq}: ...`.
    // review-swarm v2 fix-soon #9: iterate to fixpoint so nested macros
    // like `\textbf{\texttt{leaq}: ...}` fully reduce (single-pass
    // would leave residue). Cap at 6 iterations so a pathological input
    // can't spin.
    let out = String(raw == null ? "" : raw);
    for (let pass = 0; pass < 6; pass++) {
      const next = out
        .replace(/\\(textbf|textit|emph|texttt|textsf|textrm)\s*\{([^{}]*)\}/g, "$2")
        .replace(/\\[a-zA-Z]+\s*\{([^{}]*)\}/g, "$1");
      if (next === out) break;
      out = next;
    }
    // Drop the LaTeX-special escape backslashes for display only — the
    // id is still computed from the cleaned form so anchor lookup
    // matches the heading's slug (markdownToHtml escapes the same way).
    return out.replace(/\\([_&%$#{}])/g, "$1");
  }

  // Extract a hierarchical TOC tree from raw LaTeX source. Each node:
  //   { level: 1|2|3, text, id, children: [...] }
  //
  // Build rules:
  //  - \section whose title matches a known filename (options.fileNames)
  //    OR matches the file-extension heuristic → L1 (file root).
  //  - Other \section + every \subsection → L2 (nested under the most
  //    recent L1; if no L1 exists yet, a synthetic "Untitled" L1 wraps it
  //    so legacy notes with bare \sections still get a tree shape).
  //  - \subsubsection → L3 (nested under the most recent L2 of its L1).
  //
  // options.fileNames (optional array of strings) overrides the
  // extension heuristic for known-good filenames passed in by the host
  // (the Notes panel passes the course's `sources` titles so renamed or
  // extension-less uploads also get treated as L1).
  function extractTOC(source, options) {
    if (!source) return [];
    var opts = options || {};
    var fileNameSet = new Set();
    if (Array.isArray(opts.fileNames)) {
      for (var i = 0; i < opts.fileNames.length; i++) {
        var fn = opts.fileNames[i];
        if (typeof fn === "string" && fn) fileNameSet.add(fn);
      }
    }
    var tree = [];
    var currentFile = null;
    var currentSection = null;
    var takenIds = Object.create(null);
    function uniqueId(base) {
      var id = base || "section";
      if (!takenIds[id]) { takenIds[id] = 1; return id; }
      var n = 1;
      while (takenIds[id + "-" + n]) n++;
      takenIds[id + "-" + n] = 1;
      return id + "-" + n;
    }
    var re = new RegExp(
      "\\\\(section|subsection|subsubsection)\\s*" + BRACE_GROUP,
      "g"
    );
    var m;
    while ((m = re.exec(source)) !== null) {
      var kind = m[1];
      var rawTitle = m[2];
      var clean = cleanHeadingTitle(rawTitle);
      var id = uniqueId(slugify(clean));
      var isFile = (kind === "section") && (
        fileNameSet.has(clean) ||
        fileNameSet.has(rawTitle) ||
        looksLikeFilename(rawTitle)
      );
      if (isFile) {
        currentFile = { level: 1, text: clean, id: id, children: [] };
        currentSection = null;
        tree.push(currentFile);
        continue;
      }
      // For subsubsection nest under the most recent subsection (L2).
      if (kind === "subsubsection") {
        if (!currentSection) {
          // Promote to L2 if no L2 anchor yet — happens when the LLM
          // emits \subsubsection without an enclosing \subsection.
          if (!currentFile) {
            currentFile = { level: 1, text: "(untitled)", id: uniqueId("untitled"), children: [] };
            tree.push(currentFile);
          }
          currentSection = { level: 2, text: clean, id: id, children: [] };
          currentFile.children.push(currentSection);
          continue;
        }
        currentSection.children.push({ level: 3, text: clean, id: id, children: [] });
        continue;
      }
      // \subsection OR a non-file \section → L2 under the current file
      // (synthesise a file root if we haven't seen one yet, so legacy
      // notes without filename wrappers still produce a tree).
      if (!currentFile) {
        currentFile = { level: 1, text: "(untitled)", id: uniqueId("untitled"), children: [] };
        tree.push(currentFile);
      }
      currentSection = { level: 2, text: clean, id: id, children: [] };
      currentFile.children.push(currentSection);
    }
    return tree;
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
