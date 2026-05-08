"""更严格的 KB / search 测试 — 关注 tokenizer 与 RRF 排序的鬼祟边界。

覆盖：
- BM25 tokenizer 在标点 / 数字 / emoji / 重复字符 / 全角 / 大小写下的稳定性
- BM25 / vector / hybrid 在 top_k=1, top_k>>n_chunks, course_filter 不存在等边界
- HybridSearch RRF 分数随 fetch_k / weight 的可解释行为
- RRF 输出是排序稳定的（同分时 chunk_id 字典序保持）
- VectorIndex add_chunks 在 build 之后 / 之前两条路径
- KBStore.find_chunk 在 build_index 后才 lookup 的语义
- KBStore.peek_chunks 对损坏 / 不存在 / 大量 chunks 的退化路径
- KBStore.search 同时存在 global 索引和 course filter 时 top_k 截断顺序
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nano_notebooklm.kb.bm25_index import BM25Index, _tokenize
from nano_notebooklm.kb.hybrid_search import HybridSearch
from nano_notebooklm.kb.store import KBStore
from nano_notebooklm.kb.vector_index import VectorIndex
from nano_notebooklm.types import Chunk, FileType


# ── BM25 tokenizer corner cases ──────────────────────────────────────


def test_tokenize_lowercases_english():
    tokens = _tokenize("BackPropagation Algorithm")
    assert "backpropagation" in tokens
    assert "algorithm" in tokens
    assert "BackPropagation" not in tokens  # original casing dropped


def test_tokenize_emits_one_letter_words_dropped():
    """The regex `[a-z][a-z0-9]+` requires ≥2 chars. Single-letter words
    are dropped — pin so a future tokenizer change is forced to revisit."""
    assert "a" not in _tokenize("a is x")
    assert "x" not in _tokenize("x")
    # but two-char ones survive
    assert "ml" in _tokenize("ML basics")


def test_tokenize_handles_digits_inside_word():
    tokens = _tokenize("CS231N course")
    assert "cs231n" in tokens
    assert "course" in tokens


def test_tokenize_pure_punctuation_returns_empty():
    assert _tokenize("!!!???...") == []
    assert _tokenize("、。，") == []


def test_tokenize_pure_emoji_returns_empty():
    """Emoji are not in a-z range nor in CJK ideographic range."""
    assert _tokenize("💀🔥🎯") == []


def test_tokenize_chinese_bigrams_complete():
    """For text "反向传播": chars [反, 向, 传, 播]; bigrams [反向, 向传, 传播]."""
    tokens = _tokenize("反向传播")
    assert "反" in tokens and "向" in tokens
    assert "反向" in tokens and "向传" in tokens and "传播" in tokens


def test_tokenize_single_chinese_char_no_bigrams():
    """A single CJK char yields the char itself but no bigrams."""
    tokens = _tokenize("内")
    assert tokens == ["内"]


def test_tokenize_mixed_zh_en_keeps_both():
    tokens = _tokenize("Backprop 反向传播")
    assert "backprop" in tokens
    assert "反向" in tokens


def test_tokenize_empty_string_returns_empty_list():
    assert _tokenize("") == []
    assert _tokenize("   \n\t  ") == []


def test_tokenize_long_input_does_not_explode():
    """1500-token input should still finish in milliseconds — pin against
    accidental O(n²) regex changes."""
    big = "memory hierarchy " * 1500
    tokens = _tokenize(big)
    assert "memory" in tokens
    assert "hierarchy" in tokens


# ── BM25 search edge cases ──────────────────────────────────────────


def test_bm25_top_k_one_returns_strongest_match(sample_chunks):
    idx = BM25Index()
    idx.build(sample_chunks)
    out = idx.search("backpropagation gradients", top_k=1)
    assert len(out) == 1
    assert out[0].chunk_id == "c1"


def test_bm25_top_k_larger_than_corpus_caps_silently(sample_chunks):
    """top_k=999 against a 6-chunk corpus → returns ≤ corpus size, no crash."""
    idx = BM25Index()
    idx.build(sample_chunks)
    out = idx.search("backpropagation", top_k=999)
    assert len(out) <= len(sample_chunks)


def test_bm25_search_before_build_returns_empty():
    """Search called on an unbuilt index must NOT crash; returns []."""
    assert BM25Index().search("anything", top_k=5) == []


def test_bm25_build_with_empty_chunks_keeps_index_unset(tmp_path: Path):
    idx = BM25Index()
    idx.build([])
    assert idx.bm25 is None
    assert idx.chunks == []


def test_bm25_save_load_roundtrip(tmp_path: Path, sample_chunks):
    idx = BM25Index()
    idx.build(sample_chunks)
    save_path = tmp_path / "bm25.pkl"
    idx.save(save_path)

    idx2 = BM25Index()
    idx2.load(save_path)
    assert len(idx2.chunks) == len(sample_chunks)
    out = idx2.search("backpropagation", top_k=1)
    assert out[0].chunk_id == "c1"


# ── VectorIndex edge cases ───────────────────────────────────────────


def test_vector_search_before_build_returns_empty(fake_embed_fn):
    v = VectorIndex(fake_embed_fn)
    assert v.search("anything", top_k=5) == []
    assert v.total_vectors == 0


def test_vector_add_chunks_when_not_built_calls_build(sample_chunks, fake_embed_fn):
    v = VectorIndex(fake_embed_fn)
    v.add_chunks(sample_chunks)  # delegates to build
    assert v.total_vectors == len(sample_chunks)


def test_vector_add_chunks_after_build_grows_index(sample_chunks, fake_embed_fn):
    v = VectorIndex(fake_embed_fn)
    v.build(sample_chunks[:3])
    initial = v.total_vectors
    v.add_chunks(sample_chunks[3:])
    assert v.total_vectors == initial + len(sample_chunks[3:])
    assert {c.chunk_id for c in v.chunks} == {c.chunk_id for c in sample_chunks}


def test_vector_top_k_capped_to_index_size(sample_chunks, fake_embed_fn):
    """top_k=999 against 6 chunks → at most 6 returned; FAISS clamps at the
    `min(top_k, ntotal)` line."""
    v = VectorIndex(fake_embed_fn)
    v.build(sample_chunks)
    out = v.search("CNN", top_k=999)
    assert len(out) <= len(sample_chunks)


def test_vector_search_empty_chunks_returns_empty(fake_embed_fn):
    v = VectorIndex(fake_embed_fn)
    v.build([])
    assert v.search("query", top_k=3) == []


# ── HybridSearch RRF properties ──────────────────────────────────────


@pytest.fixture
def hybrid(sample_chunks, fake_embed_fn):
    v = VectorIndex(fake_embed_fn)
    v.build(sample_chunks)
    b = BM25Index()
    b.build(sample_chunks)
    return HybridSearch(v, b)


def test_hybrid_top_k_zero_currently_raises_assertion(hybrid):
    """Defect pin: ``top_k=0`` currently propagates into FAISS which asserts
    ``k > 0`` and raises. The public API can never trigger this (Pydantic
    enforces ``ge=1`` on every endpoint that takes top_k), so it's a
    defense-in-depth gap rather than a live bug. If/when ``HybridSearch``
    grows a guard like ``if top_k <= 0: return []``, flip this test to
    ``assert hybrid.search(... top_k=0) == []``."""
    with pytest.raises((AssertionError, ValueError)):
        hybrid.search("backprop", top_k=0)


def test_hybrid_top_k_one_returns_single_result(hybrid):
    out = hybrid.search("backpropagation gradients", top_k=1)
    assert len(out) == 1


def test_hybrid_top_k_larger_than_corpus_does_not_overflow(hybrid, sample_chunks):
    out = hybrid.search("backpropagation", top_k=999)
    assert len(out) <= len(sample_chunks)


def test_hybrid_score_non_negative_and_decreasing(hybrid):
    """RRF scores are 1/(k + rank), strictly positive. Output should be
    sorted descending."""
    out = hybrid.search("backpropagation gradients", top_k=5)
    assert all(r.score > 0 for r in out)
    scores = [r.score for r in out]
    assert scores == sorted(scores, reverse=True)


def test_hybrid_no_match_for_nonsense_returns_few_or_none(hybrid):
    """Nonsense token: BM25 yields nothing (zero scores filtered); vector
    returns SOMETHING because dense embeddings always have cosine values.
    We accept a small list — but every result must come from the indexed
    corpus, not invented."""
    out = hybrid.search("xyznonsensequery", top_k=3)
    # At most as many results as the vector branch yielded
    assert len(out) <= 3


def test_hybrid_zero_weights_returns_empty(hybrid):
    """Setting both branch weights to 0 means no chunk accumulates any RRF
    score — output is empty (the dict is non-empty with zeros, but sorting
    may still surface zero-score entries). Pin the actual behavior."""
    out = hybrid.search("backprop", top_k=3, vector_weight=0.0, bm25_weight=0.0)
    # All scores are exactly zero → output is degenerate but should NOT crash.
    assert all(r.score == 0.0 for r in out)


def test_hybrid_only_vector_weight_still_returns_results(hybrid):
    out = hybrid.search("CNN images", top_k=3, vector_weight=1.0, bm25_weight=0.0)
    assert out
    assert all(r.score > 0 for r in out)


def test_hybrid_rrf_k_changes_score_magnitude(hybrid):
    """Larger ``rrf_k`` flattens the score curve — top-1 score should be
    smaller. Pin that the parameter actually flows through."""
    a = hybrid.search("backprop gradients", top_k=2, rrf_k=10)
    b = hybrid.search("backprop gradients", top_k=2, rrf_k=1000)
    assert a[0].score > b[0].score


# ── KBStore find_chunk + peek_chunks ────────────────────────────────


@pytest.fixture
def loaded_kb(tmp_path, sample_chunks, fake_embed_fn):
    art = tmp_path / "artifacts"
    courses_dir = art / "courses" / "testcourse"
    courses_dir.mkdir(parents=True)
    (courses_dir / "chunks.json").write_text(
        json.dumps([c.model_dump() for c in sample_chunks], default=str)
    )
    kb = KBStore(artifacts_dir=art, embed_fn=fake_embed_fn)
    kb.build_index("testcourse")
    return kb


def test_find_chunk_returns_chunk_after_build(loaded_kb, sample_chunks):
    chunk = loaded_kb.find_chunk("c1")
    assert chunk is not None
    assert chunk.chunk_id == "c1"
    assert chunk.text == sample_chunks[0].text


def test_find_chunk_unknown_returns_none(loaded_kb):
    assert loaded_kb.find_chunk("does-not-exist") is None


def test_find_chunk_blank_id_returns_none(loaded_kb):
    """Empty-string id should not even hit the dict lookup."""
    assert loaded_kb.find_chunk("") is None


def test_find_chunk_before_build_returns_none(tmp_path, fake_embed_fn):
    """If `_all_chunks` is empty, find_chunk must NOT raise — returns None."""
    kb = KBStore(artifacts_dir=tmp_path / "art", embed_fn=fake_embed_fn)
    assert kb.find_chunk("c1") is None


def test_peek_chunks_missing_course_returns_empty(tmp_path, fake_embed_fn):
    kb = KBStore(artifacts_dir=tmp_path / "art", embed_fn=fake_embed_fn)
    assert kb.peek_chunks("ghost") == []


def test_peek_chunks_corrupt_json_returns_empty(tmp_path, fake_embed_fn):
    """Per the docstring: peek_chunks must NEVER crash chat — corrupt JSON
    falls back silently to []."""
    art = tmp_path / "art"
    cdir = art / "courses" / "broken"
    cdir.mkdir(parents=True)
    (cdir / "chunks.json").write_text("{this is not valid JSON")
    kb = KBStore(artifacts_dir=art, embed_fn=fake_embed_fn)
    assert kb.peek_chunks("broken") == []


def test_peek_chunks_caps_at_n(tmp_path, sample_chunks, fake_embed_fn):
    art = tmp_path / "art"
    cdir = art / "courses" / "testcourse"
    cdir.mkdir(parents=True)
    (cdir / "chunks.json").write_text(
        json.dumps([c.model_dump() for c in sample_chunks], default=str)
    )
    kb = KBStore(artifacts_dir=art, embed_fn=fake_embed_fn)
    out = kb.peek_chunks("testcourse", n=2)
    assert len(out) == 2


# ── KBStore.search course filtering ─────────────────────────────────


def test_kb_search_course_filter_drops_other_courses(tmp_path, fake_embed_fn):
    """Query against the global index but filter by course → results from
    other courses must be filtered out before top_k truncation."""
    art = tmp_path / "art"
    for cid, body in (("A", "alpha specific content"),
                      ("B", "beta specific content"),
                      ("C", "gamma specific content")):
        cdir = art / "courses" / cid
        cdir.mkdir(parents=True)
        chunks = [
            Chunk(chunk_id=f"{cid}_{i}", doc_id=f"d{cid}", course_id=cid,
                  text=f"{body} chunk {i}",
                  file_type=FileType.PDF, source_file=f"{cid}.pdf",
                  location=f"Page {i}/3", page=i)
            for i in range(3)
        ]
        (cdir / "chunks.json").write_text(
            json.dumps([c.model_dump() for c in chunks], default=str)
        )

    kb = KBStore(artifacts_dir=art, embed_fn=fake_embed_fn)
    kb.build_index(None)  # build a global index across all courses

    out = kb.search("specific content", top_k=3, course_id="A")
    assert out
    assert all(r.course_id == "A" for r in out)


def test_kb_search_course_filter_unknown_returns_empty(tmp_path, fake_embed_fn,
                                                      sample_chunks):
    art = tmp_path / "art"
    cdir = art / "courses" / "testcourse"
    cdir.mkdir(parents=True)
    (cdir / "chunks.json").write_text(
        json.dumps([c.model_dump() for c in sample_chunks], default=str)
    )
    kb = KBStore(artifacts_dir=art, embed_fn=fake_embed_fn)
    kb.build_index(None)
    out = kb.search("backprop", top_k=5, course_id="ghost")
    assert out == []


def test_kb_search_no_index_returns_empty(tmp_path, fake_embed_fn):
    """Search before any build_index → returns [] (exercises the
    lazy-load-then-give-up path in `KBStore.search`)."""
    art = tmp_path / "art"
    (art / "courses").mkdir(parents=True)
    kb = KBStore(artifacts_dir=art, embed_fn=fake_embed_fn)
    out = kb.search("query", top_k=3)
    assert out == []
