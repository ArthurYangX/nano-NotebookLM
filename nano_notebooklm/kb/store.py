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

    Uses OPENAI_BASE_URL / OPENAI_API_KEY from config. Vectors are L2-normalized to
    match the local backend so cosine similarity (FAISS IP) stays correct.
    """
    import openai

    if not config.OPENAI_API_KEY:
        raise RuntimeError(
            "EMBEDDING_MODE=api requires OPENAI_API_KEY. Set it in .env or switch to local."
        )

    client = openai.OpenAI(api_key=config.OPENAI_API_KEY, base_url=config.OPENAI_BASE_URL)
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


class KBStore:
    """Central knowledge base managing documents, chunks, and search indices."""

    def __init__(self, artifacts_dir: str | Path | None = None, embed_fn: Callable | None = None):
        self.artifacts_dir = Path(artifacts_dir or config.ARTIFACTS_DIR)
        self._embed_fn = embed_fn
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

    def ingest_course(
        self,
        course_dir: str | Path,
        course_id: str | None = None,
        engine: str = "pymupdf",
        lang: str = "ch",
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

        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}")) as progress:
            task = progress.add_task(f"Ingesting {course_id}...", total=len(files))
            for filepath in files:
                try:
                    pages: list[PageInfo] | None = None
                    if engine == "mineru" and filepath.suffix.lower() == ".pdf":
                        pages = mineru_batch_results.get(str(filepath.resolve()))
                        file_type = FileType.PDF
                    if pages is None:
                        pages, file_type = extract_file(filepath, engine=engine, lang=lang)
                    if not pages:
                        progress.advance(task)
                        continue

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

    def build_index(self, course_id: str | None = None):
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
        """
        index_dir = self.artifacts_dir / "indices"

        # review-swarm fix-all v2 (2026-05-16): load cached embeddings
        # from the previously-saved global index so unchanged chunks
        # (same chunk_id) reuse their vectors instead of being re-embed
        # via the API. Before this fix, every upload triggered a full
        # re-embed of all 10k chunks at ~60s/batch through codex proxy
        # = ~2.5 hours. With cache, a 374-chunk delta uploads in ~6 min.
        global_cache_dir = index_dir / "faiss" / "global"
        cached_global = VectorIndex.load_cached_vectors(global_cache_dir)

        if course_id:
            course_chunks = self._load_all_chunks(course_id)
            if course_chunks:
                # Per-course rebuild also benefits from the same cache —
                # course's own chunks may already be in the global cache
                # from a prior rebuild.
                course_cache_dir = index_dir / "faiss" / course_id
                cached_course = VectorIndex.load_cached_vectors(course_cache_dir)
                # Merge global cache as fallback (covers chunks that
                # exist globally but aren't yet in this course's per-
                # course saved index — e.g. first time this course is
                # rebuilt after a global rebuild created them).
                merged_cache = {**cached_global, **cached_course}
                course_vector = VectorIndex(self.embed_fn)
                course_vector.build(course_chunks, cached_vectors=merged_cache)
                course_vector.save(course_cache_dir)
                # Pull freshly-embedded vectors so the global build
                # below can reuse them instead of re-embedding the same
                # chunks a second time. Without this, the per-course +
                # global rebuild pattern double-pays for new chunks.
                cached_global.update(VectorIndex.load_cached_vectors(course_cache_dir))
                course_bm25 = BM25Index()
                course_bm25.build(course_chunks)
                course_bm25.save(index_dir / "bm25" / f"{course_id}.json")

        all_chunks = self._load_all_chunks(None)
        if not all_chunks:
            logger.warning("No chunks to index")
            return

        self._all_chunks = all_chunks

        logger.info(f"Building global vector index for {len(all_chunks)} chunks...")
        self._vector_index = VectorIndex(self.embed_fn)
        self._vector_index.build(all_chunks, cached_vectors=cached_global)

        logger.info("Building global BM25 index...")
        self._bm25_index = BM25Index()
        self._bm25_index.build(all_chunks)

        self._hybrid = HybridSearch(self._vector_index, self._bm25_index)

        # Invalidate find_chunk cache — _all_chunks just changed.
        self._chunk_index = None

        self._vector_index.save(index_dir / "faiss" / "global")
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

        faiss_dir = index_dir / "faiss" / suffix
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
