"""Hybrid search combining vector (semantic) and BM25 (keyword) results via RRF."""

from __future__ import annotations

from collections import defaultdict

from nano_notebooklm import config
from nano_notebooklm.kb.bm25_index import BM25Index
from nano_notebooklm.kb.vector_index import VectorIndex
from nano_notebooklm.types import SearchResult


class HybridSearch:
    """Combines vector search and BM25 search using Reciprocal Rank Fusion."""

    def __init__(self, vector_index: VectorIndex, bm25_index: BM25Index):
        self.vector_index = vector_index
        self.bm25_index = bm25_index

    def search(
        self,
        query: str,
        top_k: int = config.DEFAULT_TOP_K,
        rrf_k: int = config.RRF_K,
        vector_weight: float = 1.0,
        bm25_weight: float = 1.0,
    ) -> list[SearchResult]:
        """Search using both methods and fuse results with RRF.

        Args:
            query: Search query string
            top_k: Number of results to return
            rrf_k: RRF constant (higher = less emphasis on top ranks)
            vector_weight: Weight for vector search results
            bm25_weight: Weight for BM25 search results
        """
        # Get results from both methods (fetch more than needed for fusion)
        fetch_k = top_k * 3
        vector_results = self.vector_index.search(query, top_k=fetch_k)
        bm25_results = self.bm25_index.search(query, top_k=fetch_k)

        # RRF fusion
        rrf_scores: dict[str, float] = defaultdict(float)
        result_map: dict[str, SearchResult] = {}

        for rank, result in enumerate(vector_results):
            rrf_scores[result.chunk_id] += vector_weight / (rrf_k + rank)
            result_map[result.chunk_id] = result

        for rank, result in enumerate(bm25_results):
            rrf_scores[result.chunk_id] += bm25_weight / (rrf_k + rank)
            if result.chunk_id not in result_map:
                result_map[result.chunk_id] = result

        # Sort by RRF score and return top_k
        sorted_ids = sorted(rrf_scores, key=lambda x: rrf_scores[x], reverse=True)[:top_k]

        return [
            SearchResult(
                chunk_id=cid,
                text=result_map[cid].text,
                source_file=result_map[cid].source_file,
                location=result_map[cid].location,
                score=rrf_scores[cid],
                course_id=result_map[cid].course_id,
            )
            for cid in sorted_ids
        ]
