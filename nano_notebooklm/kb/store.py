"""Unified Knowledge Base store — manages courses, chunks, indices."""

from __future__ import annotations

import json
import logging
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
from nano_notebooklm.types import Chunk, Course, Document, SearchResult
from nano_notebooklm.utils.file_hash import sha256_file

logger = logging.getLogger(__name__)


def _get_default_embed_fn() -> Callable[[list[str]], np.ndarray]:
    """Create the default embedding function based on config."""
    mode = (config.EMBEDDING_MODE or "local").lower()
    if mode == "local":
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer(config.EMBEDDING_MODEL)

        def embed(texts: list[str]) -> np.ndarray:
            return model.encode(texts, normalize_embeddings=True, show_progress_bar=False)

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
    # Common API embedding models default; codex proxy may not support local sentence-transformers names
    if model_name in ("all-MiniLM-L6-v2", ""):  # heuristic: default for local mode → fall back to OpenAI default
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

    def ingest_course(self, course_dir: str | Path, course_id: str | None = None) -> Course:
        """Ingest all documents from a course directory."""
        course_dir = Path(course_dir)
        if course_id is None:
            course_id = course_dir.name

        course_artifacts = self.artifacts_dir / "courses" / course_id
        course_artifacts.mkdir(parents=True, exist_ok=True)

        files = collect_files(course_dir)
        logger.info(f"Found {len(files)} files in {course_id}")

        # Check for incremental updates
        hash_cache = course_artifacts / "file_hashes.json"
        changeset = detect_changes(files, course_dir, hash_cache)

        if not changeset.has_changes and (course_artifacts / "chunks.json").exists():
            logger.info(f"No changes detected for {course_id}, loading cached chunks")
            return self._load_course(course_id)

        all_chunks: list[Chunk] = []
        doc_ids: list[str] = []

        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}")) as progress:
            task = progress.add_task(f"Ingesting {course_id}...", total=len(files))
            for filepath in files:
                try:
                    pages, file_type = extract_file(filepath)
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

        if course_id:
            course_chunks = self._load_all_chunks(course_id)
            if course_chunks:
                course_vector = VectorIndex(self.embed_fn)
                course_vector.build(course_chunks)
                course_vector.save(index_dir / "faiss" / course_id)
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
        self._vector_index.build(all_chunks)

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

        if faiss_dir.exists() and bm25_path.exists():
            self._vector_index = VectorIndex(self.embed_fn)
            self._vector_index.load(faiss_dir)

            self._bm25_index = BM25Index()
            self._bm25_index.load(bm25_path)

            self._hybrid = HybridSearch(self._vector_index, self._bm25_index)
            self._all_chunks = self._vector_index.chunks
            self._chunk_index = None  # Lazy rebuild on next find_chunk call.
            logger.info(f"Loaded indices: {self._vector_index.total_vectors} vectors")
