"""FAISS-based vector index for semantic search.

Adapted from NLPProject/server_scripts/rag_pipeline.py DocumentIndex class.
Key changes: accepts embed_fn callable instead of hardcoded SentenceTransformer,
supports incremental add_chunks(), stores full Chunk metadata.
"""

from __future__ import annotations

import json
import pickle
from pathlib import Path
from typing import Callable

import faiss
import numpy as np

from nano_notebooklm.types import Chunk, SearchResult


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

    def build(self, chunks: list[Chunk], batch_size: int = 64):
        """Build index from a list of chunks."""
        if not chunks:
            return

        self.chunks = list(chunks)
        texts = [c.text for c in chunks]

        # Encode in batches
        all_embeddings = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            emb = self.embed_fn(batch)
            all_embeddings.append(emb)

        embeddings = np.vstack(all_embeddings).astype(np.float32)
        self._dim = embeddings.shape[1]

        # Inner Product index (cosine similarity with normalized vectors)
        self.index = faiss.IndexFlatIP(self._dim)
        self.index.add(embeddings)

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

    @property
    def total_vectors(self) -> int:
        return self.index.ntotal if self.index else 0
