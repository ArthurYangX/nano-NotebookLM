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
