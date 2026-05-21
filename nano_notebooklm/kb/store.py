"""Unified Knowledge Base store — manages courses, chunks, indices."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Callable

import numpy as np
from rich.progress import Progress, SpinnerColumn, TextColumn

from nano_notebooklm import config
from nano_notebooklm.ingest.chunker import chunk_pages
from nano_notebooklm.ingest.extractors import collect_files, extract_file
from nano_notebooklm.ingest.incremental import ChangeSet, detect_changes, save_hashes
from nano_notebooklm.kb.bm25_index import BM25Index
from nano_notebooklm.kb.hybrid_search import HybridSearch
from nano_notebooklm.kb.vector_index import VectorIndex
from nano_notebooklm.types import Chunk, Course, Document, FileType, PageInfo, SearchResult
from nano_notebooklm.utils.file_hash import sha256_file

logger = logging.getLogger(__name__)


def _get_default_embed_fn() -> Callable[[list[str]], np.ndarray]:
    """Create the default embedding function based on config."""
    mode = (config.EMBEDDING_MODE or "local").lower()
    if mode == "local":
        from sentence_transformers import SentenceTransformer
        # 2026-05-13: prefer MPS on Apple Silicon — bge-base-zh-v1.5
        # (110M params) runs ~20-40× faster on M-series MPS than CPU.
        # Falls back to CPU automatically on Intel Macs / Linux without
        # CUDA. Override via NANO_NLM_EMBED_DEVICE if you want to pin.
        device = os.getenv("NANO_NLM_EMBED_DEVICE")
        if not device:
            try:
                import torch
                if torch.backends.mps.is_available():
                    device = "mps"
                elif torch.cuda.is_available():
                    device = "cuda"
                else:
                    device = "cpu"
            except Exception:
                device = "cpu"
        logger.info("EMBEDDING_MODE=local: loading %s on device=%s", config.EMBEDDING_MODEL, device)
        model = SentenceTransformer(config.EMBEDDING_MODEL, device=device)
        # MPS pays per-batch sync; default batch_size=32 throttles to ~30
        # chunks/s on M-series for 512-token chunks. Bump to 128 — empirical
        # sweet spot before unified memory pressure makes things worse.
        _embed_batch_size = 128 if device == "mps" else 64

        def embed(texts: list[str]) -> np.ndarray:
            return model.encode(
                texts,
                normalize_embeddings=True,
                show_progress_bar=False,
                batch_size=_embed_batch_size,
            )

        return embed

    if mode == "api":
        return _build_api_embed_fn()

    raise ValueError(f"Unknown EMBEDDING_MODE: {config.EMBEDDING_MODE!r} (expected 'local' or 'api')")


def _build_api_embed_fn() -> Callable[[list[str]], np.ndarray]:
    """Build an embedding function backed by an OpenAI-compatible /embeddings endpoint.

    Uses EMBEDDING_API_KEY / EMBEDDING_API_BASE_URL from config (fall back to
    OPENAI_*). Vectors are L2-normalized to match the local backend so cosine
    similarity (FAISS IP) stays correct.

    2026-05-17: split out from OPENAI_* because DeepSeek chat backend doesn't
    expose /v1/embeddings — embedding stays on the codex proxy by default.
    """
    import openai

    embed_key = config.EMBEDDING_API_KEY
    embed_url = config.EMBEDDING_API_BASE_URL
    if not embed_key:
        raise RuntimeError(
            "EMBEDDING_MODE=api requires EMBEDDING_API_KEY (or OPENAI_API_KEY as fallback). "
            "Set it in .env or switch to local."
        )

    client = openai.OpenAI(api_key=embed_key, base_url=embed_url)
    model_name = config.EMBEDDING_MODEL
    # 2026-05-13: extend fallback to also catch the multilingual MiniLM
    # name — config.py defaults to that under EMBEDDING_MODE=local but a
    # stale EMBEDDING_MODEL env var pointing at a local model name in
    # API mode should still resolve to a real OpenAI-compatible model.
    if model_name in ("all-MiniLM-L6-v2", "paraphrase-multilingual-MiniLM-L12-v2", ""):
        model_name = "text-embedding-3-small"
        logger.info("EMBEDDING_MODE=api: defaulting model to %s", model_name)

    def embed(texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        # Most providers accept up to ~2048 inputs per call; chunk to be safe.
        out: list[list[float]] = []
        batch_size = 64
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            resp = client.embeddings.create(model=model_name, input=batch)
            out.extend(d.embedding for d in resp.data)
        arr = np.asarray(out, dtype=np.float32)
        # L2-normalize so cosine similarity ↔ inner product
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return arr / norms

    return embed


def migrate_legacy_faiss_layout(artifacts_dir: str | Path | None = None) -> dict:
    """One-shot: indices/faiss/{global,course_id} → indices/faiss/<preset>/...

    Older builds (pre-preset namespacing) stored FAISS directly under
    ``indices/faiss/<suffix>/``. The new layout interposes a preset segment
    so per-preset caches don't collide. On first startup after the upgrade
    we move existing bare directories under the active preset namespace so
    the user doesn't lose their indexed corpus.

    Idempotent per-entry: each remaining bare directory is migrated on its
    own; we never short-circuit on "preset dir already exists" because that
    could leave one legacy dir behind after a partial migration (review-
    swarm fix-all #M4). Symlinks are skipped (#M2 — symlink under indices/
    pointing outside the artifacts tree must not be followed by rename/
    move). Cross-mount renames fall back to ``shutil.move``.
    """
    import shutil as _shutil

    root = Path(artifacts_dir or config.ARTIFACTS_DIR) / "indices" / "faiss"
    if not root.exists():
        return {"moved": [], "skipped": "no faiss dir"}

    active = config.active_preset_id()
    if active == "custom":
        # Operator pinned EMBEDDING_MODEL to a non-preset value via env —
        # we can't safely guess which preset owns the legacy indices. Leave
        # them in place; the operator can rename the directory manually.
        return {"moved": [], "skipped": "custom preset"}

    preset_dir = root / active
    # Anything at root not named after a known preset id (incl. "custom") is
    # a legacy bare-suffix directory. Skip symlinks defensively.
    legacy_dirs = [
        p for p in root.iterdir()
        if p.is_dir()
        and not p.is_symlink()
        and p.name not in config.EMBEDDING_PRESETS
        and p.name != "custom"
    ]
    if not legacy_dirs:
        return {"moved": [], "skipped": "already migrated"}

    preset_dir.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    skipped: list[str] = []
    for src in legacy_dirs:
        dst = preset_dir / src.name
        # Defensive path-resolve check: dst must stay inside preset_dir
        # (#M2 symlink/traversal guard). resolve() collapses ``..``.
        try:
            dst.resolve().relative_to(preset_dir.resolve())
        except ValueError:
            logger.warning(
                "migrate_legacy_faiss_layout: %s resolves outside preset dir; skipping",
                dst,
            )
            skipped.append(src.name)
            continue
        if dst.exists():
            logger.info(
                "migrate_legacy_faiss_layout: dst %s already exists; leaving %s in place",
                dst, src,
            )
            skipped.append(src.name)
            continue
        try:
            _shutil.move(str(src), str(dst))
            moved.append(src.name)
        except OSError as exc:
            logger.warning(
                "migrate_legacy_faiss_layout: failed to move %s → %s (%s); leaving in place",
                src, dst, type(exc).__name__,
            )
            skipped.append(src.name)
    logger.info(
        "migrate_legacy_faiss_layout: moved %d / skipped %d legacy dir(s) under %s/",
        len(moved), len(skipped), active,
    )
    return {"moved": moved, "skipped_dirs": skipped, "target_preset": active}


class KBStore:
    """Central knowledge base managing documents, chunks, and search indices."""

    def __init__(self, artifacts_dir: str | Path | None = None, embed_fn: Callable | None = None):
        self.artifacts_dir = Path(artifacts_dir or config.ARTIFACTS_DIR)
        self._embed_fn = embed_fn
        # If caller passed a custom embed_fn (test fixtures, scripts), pin
        # the active preset to whatever they're using — we never auto-switch
        # the path namespace under a caller that injected their own fn.
        self._embed_fn_pinned = embed_fn is not None
        self._vector_index: VectorIndex | None = None
        self._bm25_index: BM25Index | None = None
        self._hybrid: HybridSearch | None = None
        self._all_chunks: list[Chunk] = []
        # chunk_id → Chunk lookup, lazily populated by find_chunk. Invalidated
        # whenever build_index runs (single-source-of-truth: _all_chunks).
        self._chunk_index: dict[str, Chunk] | None = None

    @property
    def embed_fn(self) -> Callable:
        if self._embed_fn is None:
            self._embed_fn = _get_default_embed_fn()
        return self._embed_fn

    def reset_embed_fn(self) -> None:
        """Drop cached embed_fn and any loaded indices.

        Called when the user switches embedding preset — next access to
        ``embed_fn`` lazy-loads the new model, and next ``search`` triggers
        a fresh ``_load_indices`` against the new preset's FAISS namespace.
        Indexes themselves are not deleted; switching back to a previously-
        used preset reads its existing files instantly.
        """
        if self._embed_fn_pinned:
            # Caller-injected embed_fn → reset would break their wiring.
            return
        self._embed_fn = None
        self._vector_index = None
        self._bm25_index = None
        self._hybrid = None
        self._all_chunks = []
        self._chunk_index = None

    def _faiss_root(self, preset_id: str | None = None) -> Path:
        """Per-preset FAISS namespace. All save/load goes through here so a
        preset switch becomes a path-route, not a destructive rebuild.

        ``preset_id`` overrides ``config.active_preset_id()`` — used by the
        embedding rebuild loop to pin writes to the preset captured at
        task-spawn time, so an intervening preset switch (review-swarm H1)
        cannot make rebuild-A's freshly embedded vectors land under
        preset-B's namespace.
        """
        active = preset_id or config.active_preset_id()
        return self.artifacts_dir / "indices" / "faiss" / active

    def ingest_course(
        self,
        course_dir: str | Path,
        course_id: str | None = None,
        engine: str = "pymupdf",
        lang: str = "ch",
        on_progress: Callable[[int, int], None] | None = None,
        on_extract_progress: Callable[[int, int], None] | None = None,
    ) -> Course:
        """Ingest all documents from a course directory.

        Args:
          engine: `pymupdf` (fast default) or `mineru` (slow, 10s/page on
            M4 CPU, but recovers LaTeX equations and tables that PyMuPDF
            destroys). Only affects `.pdf` files; other types use their
            native extractor either way.
          lang: passed to mineru when `engine='mineru'`. `ch` or `en`.
        """
        course_dir = Path(course_dir)
        if course_id is None:
            course_id = course_dir.name

        course_artifacts = self.artifacts_dir / "courses" / course_id
        course_artifacts.mkdir(parents=True, exist_ok=True)

        files = collect_files(course_dir)
        logger.info(f"Found {len(files)} files in {course_id}")

        # Check for incremental updates. Engine choice is part of the
        # cache key — switching from pymupdf to mineru should invalidate
        # the cache so the user actually gets the better extraction.
        hash_cache = course_artifacts / "file_hashes.json"
        changeset = detect_changes(files, course_dir, hash_cache)

        engine_marker = course_artifacts / ".extract_engine"
        # R5 fix (review-swarm fix-all v1): a corrupted (non-utf-8 / partial
        # write) marker shouldn't abort the whole ingest. Fall through to
        # default and re-extract; the marker will be overwritten cleanly.
        try:
            prev_engine = (
                engine_marker.read_text().strip() if engine_marker.exists() else "pymupdf"
            )
        except (OSError, UnicodeDecodeError):
            logger.warning(
                "engine marker at %s is corrupted; treating as pymupdf", engine_marker
            )
            prev_engine = "pymupdf"
        engine_changed = prev_engine != engine

        if not changeset.has_changes and not engine_changed and (course_artifacts / "chunks.json").exists():
            logger.info(f"No changes detected for {course_id}, loading cached chunks")
            return self._load_course(course_id)
        if engine_changed:
            logger.info(f"Extract engine changed: {prev_engine} → {engine}, re-extracting {course_id}")

        all_chunks: list[Chunk] = []
        doc_ids: list[str] = []

        # H5 fix (review-swarm fix-all v1): if engine=mineru, batch ALL
        # PDFs through a single mineru subprocess instead of per-file.
        # Saves the ~50s model-load overhead for every extra PDF beyond
        # the first. Non-PDF files (pptx/docx/md/txt) still go through
        # extract_file individually since they don't use mineru anyway.
        total_files = len(files)
        # fix-all v1 stage-split: emit extracting 0% up-front so the UI
        # shows the new "Extracting" stage starting immediately, even
        # before the mineru batch returns.
        if on_extract_progress:
            try:
                on_extract_progress(0, max(total_files, 1))
            except Exception:
                logger.warning("on_extract_progress raised at start; suppressed", exc_info=True)

        mineru_batch_results: dict[str, list[PageInfo]] = {}
        if engine == "mineru":
            pdf_files = [f for f in files if f.suffix.lower() == ".pdf"]
            if pdf_files:
                from nano_notebooklm.ingest.extractors_mineru import (
                    extract_pdfs_mineru_batch,
                    MinerUExtractionError,
                )
                logger.info(
                    "mineru engine: batch-extracting %d PDFs in one subprocess",
                    len(pdf_files),
                )
                try:
                    mineru_batch_results = extract_pdfs_mineru_batch(
                        [str(p) for p in pdf_files], lang=lang,
                    )
                except (MinerUExtractionError, FileNotFoundError) as exc:
                    # Batch failed wholesale → fall back to per-file
                    # extraction below (which may itself crash per-file,
                    # but at least one bad PDF won't take the others down).
                    logger.warning(
                        "mineru batch failed (%s); falling back to per-file extraction",
                        type(exc).__name__,
                    )
                # Mineru batch is "all or nothing" — once it returns,
                # extraction for every PDF in the batch is effectively
                # done. Non-PDFs still extract per-file below; their
                # progress increments through the per-file loop.
                if on_extract_progress and mineru_batch_results:
                    try:
                        on_extract_progress(len(pdf_files), max(total_files, 1))
                    except Exception:
                        logger.warning("on_extract_progress raised after mineru batch; suppressed", exc_info=True)

        if on_progress:
            try:
                on_progress(0, max(total_files, 1))
            except Exception:
                logger.warning("on_progress raised at start; suppressed", exc_info=True)
        # fix-all v2 M3: track files that actually yielded pages so we
        # can warn-log when the final force-tick claims completion for
        # skipped files. Surfacing the skip count to the progress bar
        # would require extending the (done,total) callback signature
        # (invasive across server.py + frontend); the warning log is
        # the minimal honest fix.
        extracted_ok = 0
        skipped_files: list[str] = []
        # fix-all v2 LOW F3: track files for which extraction has completed
        # via an explicit counter instead of relying on the loop index `i`
        # being cumulative across both the mineru-batched PDFs and the
        # per-file loop. A future refactor (e.g. `for i, filepath in
        # enumerate(non_pdf_files):`) would silently double-count without
        # this decoupling. Initialized to the number of files already
        # accounted for by the mineru batch tick above.
        extracted_so_far = len(mineru_batch_results) if mineru_batch_results else 0
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}")) as progress:
            task = progress.add_task(f"Ingesting {course_id}...", total=total_files)
            for i, filepath in enumerate(files, start=1):
                try:
                    pages: list[PageInfo] | None = None
                    extracted_in_loop = False
                    if engine == "mineru" and filepath.suffix.lower() == ".pdf":
                        pages = mineru_batch_results.get(str(filepath.resolve()))
                        file_type = FileType.PDF
                    if pages is None:
                        pages, file_type = extract_file(filepath, engine=engine, lang=lang)
                        extracted_in_loop = True
                    if not pages:
                        skipped_files.append(filepath.name)
                        progress.advance(task)
                        continue
                    extracted_ok += 1

                    # Tick extracting AFTER extract_file (pymupdf or any
                    # non-PDF). PDFs that came from the mineru batch had
                    # their tick already accounted for above.
                    # fix-all v2 LOW F3: pass `extracted_so_far` (count of
                    # files for which extraction has completed) rather
                    # than `i` (raw loop index) so a future loop refactor
                    # over a filtered list can't silently double-count.
                    if extracted_in_loop and on_extract_progress:
                        extracted_so_far += 1
                        try:
                            on_extract_progress(extracted_so_far, max(total_files, 1))
                        except Exception:
                            logger.warning("on_extract_progress raised mid-loop; suppressed", exc_info=True)

                    doc_id = sha256_file(filepath)[:16]
                    rel_path = str(filepath.relative_to(course_dir))

                    chunks = chunk_pages(
                        pages=pages,
                        source_file=rel_path,
                        file_type=file_type,
                        course_id=course_id,
                        doc_id=doc_id,
                    )
                    all_chunks.extend(chunks)
                    doc_ids.append(doc_id)

                except Exception as e:
                    logger.warning(f"Failed to process {filepath}: {e}")
                finally:
                    progress.advance(task)
                    if on_progress:
                        try:
                            on_progress(i, max(total_files, 1))
                        except Exception:
                            logger.warning("on_progress raised mid-loop; suppressed", exc_info=True)

        # Force extracting 100% in case the per-file ticks under-counted
        # (some files yielded no pages and got `continue`d above without
        # ticking). The chunking phase emits its own 100% via on_progress
        # on the final iteration regardless.
        # fix-all v2 M3: this counts ATTEMPTED files, not files that
        # actually produced pages — a file that returned no pages is
        # silently rolled into the 100% tick. The frontend bar can't
        # reflect the skip count without extending the (done,total)
        # callback signature, so we log instead.
        if on_extract_progress:
            try:
                on_extract_progress(total_files, max(total_files, 1))
            except Exception:
                logger.warning("on_extract_progress raised at end; suppressed", exc_info=True)
        if extracted_ok < total_files:
            logger.warning(
                "extracting: %d / %d files yielded no pages — skipped: %s",
                total_files - extracted_ok, total_files,
                ", ".join(skipped_files[:10]) + ("..." if len(skipped_files) > 10 else ""),
            )

        # Save chunks
        chunks_path = course_artifacts / "chunks.json"
        with open(chunks_path, "w", encoding="utf-8") as f:
            json.dump([c.model_dump() for c in all_chunks], f, ensure_ascii=False, default=str)

        # M4 fix (review-swarm fix-all v1): write the engine marker BEFORE
        # save_hashes, not after. The marker is the cache-bust signal — if
        # we crash between save_hashes and engine_marker.write_text, next
        # ingest reads the old engine and skips re-extraction even though
        # the chunks already reflect the new engine. Writing marker first
        # means a crash here leaves marker=new + hashes=old → next ingest
        # sees engine_changed=False but also sees changes via hash_cache,
        # so it re-extracts. Either order can leave a tiny inconsistency,
        # but marker-first plus the hash-cache safety net converges to
        # "re-extract on any doubt", which is the safe behavior.
        engine_marker.write_text(engine)
        # Save file hashes for future incremental updates
        save_hashes(files, course_dir, hash_cache)

        course = Course(course_id=course_id, name=course_id, documents=doc_ids)
        meta_path = course_artifacts / "course_meta.json"
        meta_path.write_text(course.model_dump_json(indent=2))

        logger.info(f"Ingested {course_id}: {len(all_chunks)} chunks from {len(files)} files")
        return course

    def build_index(
        self,
        course_id: str | None = None,
        on_embed_progress: Callable[[int, int], None] | None = None,
        *,
        preset_id: str | None = None,
        skip_bm25: bool = False,
        skip_global: bool = False,
    ):
        """Build search indices.

        review-swarm fix-all v3 #C7: a single-course rebuild used to
        overwrite the global in-memory hybrid index with only that course's
        chunks, so a fresh `/api/upload/<X>` silently broke search for every
        other course. Behaviour now:

        - When ``course_id`` is given, also persist that course's
          per-course on-disk index (so future selective loads work).
        - In every code path, recompute the in-memory hybrid index from the
          full ``_load_all_chunks(None)`` set and persist it as the global
          index. Subsequent ``search`` calls always see the union of
          courses regardless of which course triggered the rebuild.

        Keyword-only args (review-swarm fix-all #H1/#M3, 2026-05-20):
          preset_id: pin FAISS writes to this preset namespace, ignoring
            ``config.active_preset_id()``. Used by the embedding rebuild
            loop so an intervening preset switch can't redirect vectors.
          skip_bm25: skip BM25 rebuild (preset-independent — pure waste
            during a preset switch where only embeddings change).
          skip_global: skip the global FAISS+BM25 rebuild and the
            in-memory hybrid index update. Used by the rebuild loop to
            avoid an N+1 global rebuild — caller does one final pass
            with skip_global=False after the loop.
        """
        index_dir = self.artifacts_dir / "indices"
        faiss_root = self._faiss_root(preset_id)

        # review-swarm fix-all v2 (2026-05-16): load cached embeddings
        # from the previously-saved global index so unchanged chunks
        # (same chunk_id) reuse their vectors instead of being re-embed
        # via the API. Before this fix, every upload triggered a full
        # re-embed of all 10k chunks at ~60s/batch through codex proxy
        # = ~2.5 hours. With cache, a 374-chunk delta uploads in ~6 min.
        global_cache_dir = faiss_root / "global"
        cached_global = VectorIndex.load_cached_vectors(global_cache_dir)

        # Pre-compute total miss count across both build phases so the
        # emitted progress is monotonic and uses a single shared denominator
        # (otherwise the bar would hit 100% during per-course build, then
        # restart from 0% for the global build).
        course_chunks_pre: list[Chunk] = []
        per_course_misses: int = 0
        # Computed once in the pre-compute pass and reused by the per-course
        # build below — avoids a second `load_cached_vectors` round-trip
        # (FAISS deserialize + meta JSON parse) for the same per-course
        # cache directory.
        course_merged_cache: dict | None = None
        merged_cache_pre: dict = cached_global
        if course_id:
            course_chunks_pre = self._load_all_chunks(course_id)
            if course_chunks_pre:
                course_cache_dir_pre = faiss_root / course_id
                cached_course_pre = VectorIndex.load_cached_vectors(course_cache_dir_pre)
                merged_cache_pre = {**cached_global, **cached_course_pre}
                course_merged_cache = merged_cache_pre
                per_course_misses = sum(1 for c in course_chunks_pre if c.chunk_id not in merged_cache_pre)
        all_chunks_pre = self._load_all_chunks(None)
        # After per-course build, its fresh vectors land in cached_global,
        # so global build's misses are roughly the chunks not yet anywhere.
        post_per_course_known = set(merged_cache_pre.keys()) | {c.chunk_id for c in course_chunks_pre}
        global_misses = sum(1 for c in all_chunks_pre if c.chunk_id not in post_per_course_known)
        total_embed_misses = max(1, per_course_misses + global_misses)
        embed_done = 0

        def _embed_cb_factory(phase_total: int):
            """Return a per-phase on_progress mapped into the global denominator.

            Must be called *after* the prior phase finished, so it captures
            the up-to-date ``embed_done`` as this phase's start offset.
            Inside ``_cb`` we (a) clamp the in-phase count to ``phase_total``
            in case the underlying build over-counts (e.g. dim-mismatch
            recursion re-embeds everything), and (b) enforce monotonicity so
            the outer bar never visually regresses. Traceback for outer-cb
            failures is logged once per phase to avoid spamming when the
            callback is consistently broken.
            """
            phase_start = embed_done
            cb_failed = [False]
            def _cb(done_in_phase: int, _total_in_phase: int):
                nonlocal embed_done
                clamped = min(max(done_in_phase, 0), max(phase_total, 0))
                new_done = min(phase_start + clamped, total_embed_misses)
                if new_done < embed_done:
                    new_done = embed_done
                embed_done = new_done
                if on_embed_progress:
                    try:
                        on_embed_progress(embed_done, total_embed_misses)
                    except Exception:
                        if not cb_failed[0]:
                            logger.warning(
                                "on_embed_progress raised; suppressing further tracebacks this phase",
                                exc_info=True,
                            )
                            cb_failed[0] = True
            return _cb

        # Kick off so the bar leaves 0% even when total_embed_misses is 0
        # (full cache hit — both builds will be instant).
        if on_embed_progress and total_embed_misses == 0:
            try:
                on_embed_progress(1, 1)
            except Exception:
                logger.warning("on_embed_progress raised at kickoff; suppressed", exc_info=True)

        if course_id:
            course_chunks = course_chunks_pre
            if course_chunks:
                # Per-course rebuild also benefits from the same cache —
                # course's own chunks may already be in the global cache
                # from a prior rebuild. We reuse the merged cache computed
                # in the pre-compute pass above (`course_merged_cache`)
                # rather than reloading the per-course FAISS index.
                course_cache_dir = faiss_root / course_id
                merged_cache = course_merged_cache if course_merged_cache is not None else cached_global
                course_vector = VectorIndex(self.embed_fn)
                course_vector.build(
                    course_chunks,
                    cached_vectors=merged_cache,
                    on_progress=_embed_cb_factory(per_course_misses),
                )
                course_vector.save(course_cache_dir)
                # Pull freshly-embedded vectors so the global build
                # below can reuse them instead of re-embedding the same
                # chunks a second time. Without this, the per-course +
                # global rebuild pattern double-pays for new chunks.
                cached_global.update(VectorIndex.load_cached_vectors(course_cache_dir))
                if not skip_bm25:
                    course_bm25 = BM25Index()
                    course_bm25.build(course_chunks)
                    course_bm25.save(index_dir / "bm25" / f"{course_id}.json")

        if skip_global:
            # Caller is doing a multi-course rebuild and will issue one
            # global pass at the end — skip the per-iteration global
            # rebuild to avoid N+1 work.
            return

        all_chunks = all_chunks_pre
        if not all_chunks:
            logger.warning("No chunks to index")
            return

        self._all_chunks = all_chunks

        logger.info(f"Building global vector index for {len(all_chunks)} chunks...")
        self._vector_index = VectorIndex(self.embed_fn)
        self._vector_index.build(
            all_chunks,
            cached_vectors=cached_global,
            on_progress=_embed_cb_factory(global_misses),
        )

        if not skip_bm25:
            logger.info("Building global BM25 index...")
            self._bm25_index = BM25Index()
            self._bm25_index.build(all_chunks)
            self._hybrid = HybridSearch(self._vector_index, self._bm25_index)
        else:
            # No BM25 update — preserve the existing global BM25 from disk
            # so hybrid search still works. _hybrid stays untouched if it
            # was already loaded; otherwise next search will lazy-load.
            if self._bm25_index is not None:
                self._hybrid = HybridSearch(self._vector_index, self._bm25_index)

        # Invalidate find_chunk cache — _all_chunks just changed.
        self._chunk_index = None

        self._vector_index.save(faiss_root / "global")
        if not skip_bm25:
            self._bm25_index.save(index_dir / "bm25" / "global.json")

        logger.info(f"Global index built: {self._vector_index.total_vectors} vectors")

    def search(self, query: str, top_k: int = config.DEFAULT_TOP_K, course_id: str | None = None) -> list[SearchResult]:
        """Search across indexed chunks.

        fix-all v3 #M8: a single-course filter previously fetched only
        ``top_k * 3`` global hits and post-filtered, which falsely reported
        zero results for small courses dominated by a larger sibling. We
        now widen the fetch progressively until we get enough course-
        matching hits or hit a generous ceiling.
        """
        # Always load global index first (covers all courses)
        if self._hybrid is None:
            self._load_indices(None)  # Load global index

        if self._hybrid is None:
            return []

        if not course_id:
            return self._hybrid.search(query, top_k=top_k)

        for fetch_k in (top_k * 3, top_k * 10, top_k * 30):
            results = self._hybrid.search(query, top_k=fetch_k)
            filtered = [r for r in results if r.course_id == course_id]
            if len(filtered) >= top_k:
                return filtered[:top_k]
            # If the global index returned fewer than fetch_k, we've
            # exhausted the corpus already — no point widening further.
            if len(results) < fetch_k:
                return filtered[:top_k]
        return filtered[:top_k]

    def get_chunks(self, course_id: str | None = None) -> list[Chunk]:
        """Get all chunks, optionally filtered by course."""
        chunks = self._load_all_chunks(course_id)
        return chunks

    def find_chunk(self, chunk_id: str) -> Chunk | None:
        """Look up a single chunk by id without scanning. The lookup dict is
        built lazily over `_all_chunks` (populated by build_index) the first
        time it's needed; on cache miss we don't fall through to a disk
        reload — that fallback exists in `get_chunks` and is multi-second
        from inside an event-loop tool handler.
        """
        if not chunk_id:
            return None
        if self._chunk_index is None and self._all_chunks:
            self._chunk_index = {c.chunk_id: c for c in self._all_chunks}
        if self._chunk_index is None:
            return None
        return self._chunk_index.get(chunk_id)

    def peek_chunks(self, course_id: str, n: int = 30) -> list[Chunk]:
        """Return up to ``n`` chunks from a course without loading the full
        chunks.json into Pydantic models.

        Used by language fingerprinting so the first chat call against a new
        course doesn't pay 100s of ms to instantiate 1500+ Chunk objects just
        to inspect 30. Reads + parses JSON, slices, then validates only the
        slice.

        On any read / parse / validation error returns ``[]`` and logs a
        warning. The previous implementation fell back to a full
        ``get_chunks`` load — that defeated the optimisation precisely when
        it was most needed (corrupt or schema-drifted chunks.json) and was
        silent. Returning an empty list lets the caller (lang fingerprint)
        default cleanly to the safe "en" fingerprint.
        """
        try:
            chunks_path = self.artifacts_dir / "courses" / course_id / "chunks.json"
            if not chunks_path.exists():
                return []
            with open(chunks_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return [Chunk(**item) for item in data[:n]]
        except Exception:  # noqa: BLE001 — fingerprint must never crash chat
            logger.warning("peek_chunks(%s, %d) failed; returning [] for safe "
                           "fingerprint default", course_id, n, exc_info=True)
            return []

    def _load_all_chunks(self, course_id: str | None = None) -> list[Chunk]:
        """Load chunks from disk."""
        courses_dir = self.artifacts_dir / "courses"
        if not courses_dir.exists():
            return []

        all_chunks = []
        dirs = [courses_dir / course_id] if course_id else sorted(courses_dir.iterdir())

        for course_dir in dirs:
            chunks_path = course_dir / "chunks.json"
            if chunks_path.exists():
                with open(chunks_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                all_chunks.extend(Chunk(**item) for item in data)

        return all_chunks

    def _load_course(self, course_id: str) -> Course:
        meta_path = self.artifacts_dir / "courses" / course_id / "course_meta.json"
        if meta_path.exists():
            return Course.model_validate_json(meta_path.read_text())
        return Course(course_id=course_id, name=course_id)

    def _load_indices(self, course_id: str | None = None):
        """Try to load pre-built indices from disk."""
        index_dir = self.artifacts_dir / "indices"
        suffix = course_id if course_id else "global"

        faiss_dir = self._faiss_root() / suffix
        bm25_path = index_dir / "bm25" / f"{suffix}.json"
        # Legacy .pkl files (pre fix-all v3 #C6) are intentionally not loaded
        # — pickle.load was the RCE risk; on a fresh build a .json sibling
        # appears next to it and supersedes the legacy file.
        # fix-all v4 #B8: visible breadcrumb so an operator who just pulled
        # this commit and sees `total_chunks=0` understands they need to
        # rebuild rather than thinking the corpus is gone.
        legacy_pkl = index_dir / "bm25" / f"{suffix}.pkl"
        if legacy_pkl.exists() and not bm25_path.exists():
            logger.warning(
                "Found legacy BM25 pickle at %s; pickle loading is disabled "
                "(fix-all v3 #C6). Rerun scripts/ingest_all.py or any "
                "/api/upload to rebuild the .json index.", legacy_pkl,
            )

        if faiss_dir.exists() and bm25_path.exists():
            self._vector_index = VectorIndex(self.embed_fn)
            self._vector_index.load(faiss_dir)

            self._bm25_index = BM25Index()
            self._bm25_index.load(bm25_path)

            self._hybrid = HybridSearch(self._vector_index, self._bm25_index)
            self._all_chunks = self._vector_index.chunks
            self._chunk_index = None  # Lazy rebuild on next find_chunk call.
            logger.info(f"Loaded indices: {self._vector_index.total_vectors} vectors")
