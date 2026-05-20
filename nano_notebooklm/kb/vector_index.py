"""FAISS-based vector index for semantic search.

Accepts an `embed_fn` callable instead of a hardcoded SentenceTransformer,
supports incremental `add_chunks()`, stores full Chunk metadata.
"""

from __future__ import annotations

import json
import logging
import pickle
from pathlib import Path
from typing import Callable

import faiss
import numpy as np

from nano_notebooklm.types import Chunk, SearchResult

logger = logging.getLogger(__name__)


class VectorIndex:
    """Manages FAISS vector index for document chunks."""

    def __init__(self, embed_fn: Callable[[list[str]], np.ndarray]):
        """
        Args:
            embed_fn: A function that takes a list of strings and returns
                      a numpy array of shape (n, dim) with normalized embeddings.
        """
        self.embed_fn = embed_fn
        self.index: faiss.Index | None = None
        self.chunks: list[Chunk] = []
        self._dim: int = 0

    def build(
        self,
        chunks: list[Chunk],
        batch_size: int = 64,
        cached_vectors: dict[str, np.ndarray] | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ):
        """Build index from a list of chunks.

        Args:
          chunks: chunks to index.
          batch_size: batch size passed to embed_fn for cache-miss chunks.
          cached_vectors: optional mapping chunk_id → embedding. When
            present, chunks whose ``chunk_id`` is in the cache reuse the
            cached vector instead of being sent to the embed API. Skips
            ~99% of API calls on incremental re-indexing (only newly-
            added or content-changed chunks need fresh API calls because
            chunk_id is content-hash-stable per the chunker).

        review-swarm fix-all v2 (2026-05-16): previously this method
        called ``embed_fn(batch)`` for EVERY chunk, so a single new
        upload triggered a 10k-chunk re-embed at ~60s/batch through the
        codex proxy = ~2.5 hours wall time. The cache-aware path drops
        that to (new_chunks // batch_size) calls — typically <10 batches
        for an incremental upload.
        """
        if not chunks:
            return

        self.chunks = list(chunks)
        cache = cached_vectors or {}

        # Partition: cached hits keep their vector; misses queue for embed.
        embeddings: list[np.ndarray | None] = [None] * len(chunks)
        miss_indices: list[int] = []
        miss_texts: list[str] = []
        for i, c in enumerate(chunks):
            cached_vec = cache.get(c.chunk_id)
            if cached_vec is not None:
                embeddings[i] = cached_vec
            else:
                miss_indices.append(i)
                miss_texts.append(c.text)

        hit_count = len(chunks) - len(miss_texts)
        if cache:
            logger.info(
                "VectorIndex cache: %d/%d hit, %d miss → %d embed batch(es)",
                hit_count, len(chunks), len(miss_texts),
                (len(miss_texts) + batch_size - 1) // batch_size,
            )

        # Embed only the misses.
        if miss_texts:
            total_miss = len(miss_texts)
            # Log the traceback once per build if the callback misbehaves —
            # otherwise a broken callback would flood the log with the same
            # stack ~160× for a 10k-chunk re-embed.
            cb_failed = [False]

            def _emit(done: int) -> None:
                if not on_progress:
                    return
                try:
                    on_progress(done, total_miss)
                except Exception:
                    if not cb_failed[0]:
                        logger.warning(
                            "on_progress raised; suppressing further tracebacks this build",
                            exc_info=True,
                        )
                        cb_failed[0] = True

            _emit(0)
            for start in range(0, total_miss, batch_size):
                batch = miss_texts[start : start + batch_size]
                batch_emb = np.asarray(self.embed_fn(batch))
                for j in range(batch_emb.shape[0]):
                    embeddings[miss_indices[start + j]] = batch_emb[j]
                _emit(min(start + batch_size, total_miss))

        # Validate uniform dim. A model swap between builds (e.g. switching
        # EMBEDDING_MODEL) makes cached vectors dim-incompatible with fresh
        # ones — detect and re-embed everything cleanly.
        shapes = {tuple(np.shape(e)) for e in embeddings if e is not None}
        if len(shapes) > 1:
            logger.warning(
                "VectorIndex: vector dim mismatch %s (likely model swap) "
                "— discarding cache and re-embedding all %d chunks",
                shapes, len(chunks),
            )
            # Don't forward on_progress: the recursive call would emit
            # `(0, all_chunks)` as a fresh denominator, which from the
            # outer caller's monotonic counter looks like a backwards jump
            # AND the new total (all chunks) likely exceeds the precomputed
            # global denominator. Outer caller's last-known progress just
            # stalls until the recursive build completes — acceptable for
            # a rare dim-swap path.
            return self.build(chunks, batch_size=batch_size, cached_vectors=None, on_progress=None)

        embeddings_arr = np.stack(
            [np.asarray(e, dtype=np.float32).reshape(-1) for e in embeddings],
            axis=0,
        ).astype(np.float32)
        self._dim = embeddings_arr.shape[1]

        # Inner Product index (cosine similarity with normalized vectors)
        self.index = faiss.IndexFlatIP(self._dim)
        self.index.add(embeddings_arr)

    def add_chunks(self, new_chunks: list[Chunk], batch_size: int = 64):
        """Incrementally add chunks to an existing index."""
        if not new_chunks:
            return

        if self.index is None:
            self.build(new_chunks, batch_size)
            return

        texts = [c.text for c in new_chunks]
        all_embeddings = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            emb = self.embed_fn(batch)
            all_embeddings.append(emb)

        embeddings = np.vstack(all_embeddings).astype(np.float32)
        self.index.add(embeddings)
        self.chunks.extend(new_chunks)

    def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        """Search for the most relevant chunks."""
        if self.index is None or self.index.ntotal == 0:
            return []

        query_emb = self.embed_fn([query]).astype(np.float32)
        scores, indices = self.index.search(query_emb, min(top_k, self.index.ntotal))

        results = []
        for score, idx in zip(scores[0], indices[0]):
            if 0 <= idx < len(self.chunks):
                chunk = self.chunks[idx]
                results.append(SearchResult(
                    chunk_id=chunk.chunk_id,
                    text=chunk.text,
                    source_file=chunk.source_file,
                    location=chunk.location,
                    score=float(score),
                    course_id=chunk.course_id,
                ))
        return results

    def save(self, save_dir: str | Path):
        """Save index and metadata to disk."""
        save_dir = Path(save_dir)
        save_dir.mkdir(parents=True, exist_ok=True)

        if self.index is not None:
            faiss.write_index(self.index, str(save_dir / "faiss.index"))

        # Save chunk metadata (without embeddings)
        chunk_data = [c.model_dump() for c in self.chunks]
        with open(save_dir / "chunks_meta.json", "w", encoding="utf-8") as f:
            json.dump(chunk_data, f, ensure_ascii=False, default=str)

    def load(self, save_dir: str | Path):
        """Load index and metadata from disk."""
        save_dir = Path(save_dir)
        index_path = save_dir / "faiss.index"
        meta_path = save_dir / "chunks_meta.json"

        if index_path.exists():
            self.index = faiss.read_index(str(index_path))

        if meta_path.exists():
            with open(meta_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.chunks = [Chunk(**item) for item in data]

    @classmethod
    def load_cached_vectors(cls, save_dir: str | Path) -> dict[str, np.ndarray]:
        """Reconstruct a chunk_id → embedding mapping from a previously-
        saved index. Used by ``kb.build_index`` to feed ``build(...)``
        and avoid re-embedding unchanged chunks.

        Returns empty dict if no saved index exists OR if the saved
        index and metadata are inconsistent (corrupt cache → safe path
        is full re-embed on next build).
        """
        save_dir = Path(save_dir)
        idx_path = save_dir / "faiss.index"
        meta_path = save_dir / "chunks_meta.json"
        if not (idx_path.exists() and meta_path.exists()):
            return {}
        try:
            index = faiss.read_index(str(idx_path))
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception as exc:
            logger.warning(
                "VectorIndex: failed to load cached vectors from %s (%s) "
                "— next build will re-embed everything",
                save_dir, type(exc).__name__,
            )
            return {}
        out: dict[str, np.ndarray] = {}
        total = index.ntotal
        for i, item in enumerate(meta):
            if i >= total:
                break
            cid = item.get("chunk_id")
            if not cid:
                continue
            # IndexFlatIP supports reconstruct(i) → (D,) float32 array.
            try:
                out[cid] = index.reconstruct(i).astype(np.float32)
            except Exception:
                continue
        logger.info(
            "VectorIndex.load_cached_vectors: %d/%d vectors loaded from %s",
            len(out), total, save_dir,
        )
        return out

    @property
    def total_vectors(self) -> int:
        return self.index.ntotal if self.index else 0
