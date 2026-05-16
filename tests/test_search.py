"""Tests for vector, BM25, and hybrid search components."""

from __future__ import annotations

from pathlib import Path

from nano_notebooklm.kb.bm25_index import BM25Index, _tokenize
from nano_notebooklm.kb.hybrid_search import HybridSearch
from nano_notebooklm.kb.vector_index import VectorIndex


def test_tokenize_handles_chinese_and_english():
    tokens = _tokenize("Backpropagation 反向传播 algorithm")
    assert "backpropagation" in tokens
    assert "algorithm" in tokens
    assert "反" in tokens and "向" in tokens
    # bigrams for Chinese
    assert "反向" in tokens or "向传" in tokens


def test_bm25_returns_relevant_results(sample_chunks):
    idx = BM25Index()
    idx.build(sample_chunks)
    results = idx.search("backpropagation gradients", top_k=3)
    assert results, "BM25 should return at least one result"
    assert results[0].chunk_id == "c1"


def test_bm25_zero_score_filtered(sample_chunks):
    idx = BM25Index()
    idx.build(sample_chunks)
    results = idx.search("xyznonsense", top_k=3)
    # All scores should be zero ⇒ no results returned
    assert results == []


def test_vector_index_build_and_search(sample_chunks, fake_embed_fn):
    v = VectorIndex(fake_embed_fn)
    v.build(sample_chunks)
    assert v.total_vectors == len(sample_chunks)
    results = v.search("CNN images", top_k=3)
    assert len(results) == 3
    # Must be a valid chunk id from the corpus
    assert all(r.chunk_id in {c.chunk_id for c in sample_chunks} for r in results)


def test_vector_save_load_roundtrip(tmp_path: Path, sample_chunks, fake_embed_fn):
    v = VectorIndex(fake_embed_fn)
    v.build(sample_chunks)
    save_dir = tmp_path / "vidx"
    v.save(save_dir)

    v2 = VectorIndex(fake_embed_fn)
    v2.load(save_dir)
    assert v2.total_vectors == len(sample_chunks)
    assert {c.chunk_id for c in v2.chunks} == {c.chunk_id for c in sample_chunks}


def test_vector_build_reuses_cached_vectors(sample_chunks, fake_embed_fn):
    """review-swarm fix-all v2 (2026-05-16): the build() method must
    consult `cached_vectors` and only call embed_fn for cache-miss
    chunks. Before this fix, every kb.build_index call re-embedded all
    10k+ chunks through the codex proxy (~2.5h wall time per upload)."""
    import numpy as np
    counter = {"calls": 0, "total_chunks": 0}

    def counting_embed(texts):
        counter["calls"] += 1
        counter["total_chunks"] += len(texts)
        return fake_embed_fn(texts)

    # 2 of 3 chunks already cached → only 1 embed batch call expected.
    cached = {
        sample_chunks[0].chunk_id: np.random.rand(32).astype(np.float32),
        sample_chunks[1].chunk_id: np.random.rand(32).astype(np.float32),
    }
    v = VectorIndex(counting_embed)
    v.build(sample_chunks[:3], cached_vectors=cached)

    assert v.total_vectors == 3, "all 3 chunks must end up in the index"
    assert counter["total_chunks"] == 1, (
        f"only 1 cache-miss chunk should be embedded, but {counter['total_chunks']} were"
    )


def test_vector_build_cache_dim_mismatch_falls_back_to_full_reembed(
    sample_chunks, fake_embed_fn
):
    """Model-swap safety: if cached vectors have a different dim than
    fresh embed_fn output, the cache is discarded and ALL chunks are
    re-embedded with consistent dim. (Cost: 1 wasted batch on the
    initial miss before the mismatch is detected — acceptable overhead
    that fires only on the rare model-swap event.)"""
    import numpy as np
    # Cache pretends to be 384-dim while fresh embed returns 32-dim.
    cached = {
        sample_chunks[0].chunk_id: np.random.rand(384).astype(np.float32),
    }
    v = VectorIndex(fake_embed_fn)
    v.build(sample_chunks[:2], cached_vectors=cached)
    # Index built successfully (no np.stack shape crash) with the
    # fresh-embed dim (32, not 384).
    assert v.total_vectors == 2
    assert v._dim == 32, (
        "fallback path must re-embed with fresh dim, not the stale "
        f"cache dim — got _dim={v._dim}"
    )


def test_vector_load_cached_vectors_roundtrip(tmp_path: Path, sample_chunks, fake_embed_fn):
    """The load_cached_vectors helper must reconstruct
    chunk_id → embedding from a previously-saved index. Used by
    kb.build_index to feed the cache."""
    v = VectorIndex(fake_embed_fn)
    v.build(sample_chunks)
    save_dir = tmp_path / "vidx"
    v.save(save_dir)

    cached = VectorIndex.load_cached_vectors(save_dir)
    assert set(cached.keys()) == {c.chunk_id for c in sample_chunks}
    # Each cached vector has the right dim.
    sample_vec = next(iter(cached.values()))
    assert sample_vec.shape == (32,)  # fake_embed_fn dim


def test_vector_load_cached_vectors_returns_empty_on_missing_dir(tmp_path: Path):
    cached = VectorIndex.load_cached_vectors(tmp_path / "does_not_exist")
    assert cached == {}


def test_hybrid_rrf_combines_both(sample_chunks, fake_embed_fn):
    v = VectorIndex(fake_embed_fn)
    v.build(sample_chunks)
    b = BM25Index()
    b.build(sample_chunks)
    h = HybridSearch(v, b)
    results = h.search("retrieval generation", top_k=3)
    assert results
    # The RAG chunk (c4) mentions both "retrieval" and "generation"
    top_ids = [r.chunk_id for r in results]
    assert "c4" in top_ids


def test_hybrid_empty_query_does_not_crash(sample_chunks, fake_embed_fn):
    v = VectorIndex(fake_embed_fn)
    v.build(sample_chunks)
    b = BM25Index()
    b.build(sample_chunks)
    h = HybridSearch(v, b)
    # Empty query: BM25 returns nothing; vector returns whatever neighbours exist
    results = h.search("", top_k=2)
    # No assertions on contents, just no crash and len ≤ top_k
    assert len(results) <= 2
