"""BM25-based keyword search index."""

from __future__ import annotations

import json
from pathlib import Path

from rank_bm25 import BM25Okapi

from nano_notebooklm.types import Chunk, SearchResult


class BM25Index:
    """BM25 keyword search over document chunks."""

    def __init__(self):
        self.bm25: BM25Okapi | None = None
        self.chunks: list[Chunk] = []
        # review-swarm fix-all v3 #C6: keep tokenized corpus around so
        # save/load can serialise it as JSON instead of pickling the
        # BM25Okapi object (pickle.load is RCE-bait if anything ever writes
        # a poisoned indices/bm25/*.json into artifacts/).
        self._tokenized: list[list[str]] | None = None

    def build(self, chunks: list[Chunk]):
        """Build BM25 index from chunks."""
        if not chunks:
            return
        self.chunks = list(chunks)
        self._tokenized = [_tokenize(c.text) for c in chunks]
        self.bm25 = BM25Okapi(self._tokenized)

    def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        """Search for relevant chunks by keyword matching."""
        if self.bm25 is None or not self.chunks:
            return []

        tokens = _tokenize(query)
        scores = self.bm25.get_scores(tokens)

        # Get top-k indices
        top_indices = scores.argsort()[::-1][:top_k]

        results = []
        for idx in top_indices:
            if scores[idx] > 0 and idx < len(self.chunks):
                chunk = self.chunks[idx]
                results.append(SearchResult(
                    chunk_id=chunk.chunk_id,
                    text=chunk.text,
                    source_file=chunk.source_file,
                    location=chunk.location,
                    score=float(scores[idx]),
                    course_id=chunk.course_id,
                ))
        return results

    def save(self, save_path: str | Path):
        """Persist tokenized corpus + chunks as JSON. The BM25Okapi instance
        itself is not serialised; ``load`` rebuilds it from the tokenized
        documents so a poisoned file can't smuggle a callable across the
        deserialisation boundary."""
        save_path = Path(save_path)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "tokenized": self._tokenized,
            "chunks": [c.model_dump() for c in self.chunks],
        }
        save_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    def load(self, save_path: str | Path):
        """Load BM25 index from disk (JSON only — pickle support removed)."""
        save_path = Path(save_path)
        data = json.loads(save_path.read_text(encoding="utf-8"))
        self.chunks = [Chunk(**item) for item in data.get("chunks", [])]
        tokenized = data.get("tokenized")
        if tokenized:
            self._tokenized = list(tokenized)
            self.bm25 = BM25Okapi(self._tokenized)
        else:
            self._tokenized = None
            self.bm25 = None


def _tokenize(text: str) -> list[str]:
    """Tokenize text for BM25 — handles both English and Chinese.

    For Chinese: splits into individual characters and bigrams (no jieba dependency).
    For English: standard word splitting.
    """
    import re
    text = text.lower()

    # Extract English words
    en_tokens = re.findall(r"[a-z][a-z0-9]+", text)

    # Extract Chinese characters and form bigrams
    cn_chars = re.findall(r"[\u4e00-\u9fff]", text)
    cn_bigrams = [cn_chars[i] + cn_chars[i + 1] for i in range(len(cn_chars) - 1)]

    return en_tokens + cn_chars + cn_bigrams
