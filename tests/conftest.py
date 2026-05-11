"""Shared fixtures for nano-NOTEBOOKLM tests.

Tests run without network access or LLM keys: we use a deterministic hash-based
embedding function so VectorIndex/HybridSearch behave consistently in CI.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import numpy as np
import pytest

from nano_notebooklm.types import Chunk, FileType


def _hash_embed(texts: list[str], dim: int = 32) -> np.ndarray:
    """Deterministic hash-based embedding — for tests only."""
    out = np.zeros((len(texts), dim), dtype=np.float32)
    for i, t in enumerate(texts):
        # Token-level hashing to give similar texts similar vectors
        for tok in t.lower().split():
            h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
            for j in range(dim):
                out[i, j] += ((h >> (j * 4)) & 0xF) / 15.0
        # Add a small content signal
        h = int(hashlib.md5(t.encode("utf-8")).hexdigest(), 16)
        for j in range(dim):
            out[i, j] += ((h >> (j * 2)) & 0x3) / 3.0
    norms = np.linalg.norm(out, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return out / norms


@pytest.fixture
def fake_embed_fn():
    return _hash_embed


@pytest.fixture
def sample_chunks() -> list[Chunk]:
    """A small mixed-language corpus for retrieval tests."""
    raw = [
        ("c1", "Backpropagation computes gradients of loss with respect to weights via chain rule.", "ml.pdf", "PDF p.1"),
        ("c2", "Convolutional neural networks use filters to extract spatial features from images.", "ml.pdf", "PDF p.2"),
        ("c3", "Reinforcement learning agents maximize expected cumulative reward via policies.", "rl.pdf", "PDF p.1"),
        ("c4", "RAG combines retrieval with generation to ground language models in documents.", "nlp.pdf", "PDF p.1"),
        ("c5", "BM25 is a keyword-based ranking function used in classical information retrieval.", "ir.pdf", "PDF p.1"),
        ("c6", "中文分词 是 中文 自然语言 处理 的 基础 任务 之一", "zh.pdf", "PDF p.1"),
    ]
    return [
        Chunk(
            chunk_id=cid,
            doc_id=cid,
            course_id="testcourse",
            text=text,
            file_type=FileType.PDF,
            source_file=src,
            location=loc,
            page=1,
        )
        for cid, text, src, loc in raw
    ]


@pytest.fixture
def isolated_artifacts(tmp_path, monkeypatch) -> Path:
    """Point ARTIFACTS_DIR at a temporary directory and reload config-using modules."""
    art = tmp_path / "artifacts"
    art.mkdir()
    monkeypatch.setenv("ARTIFACTS_DIR", str(art))
    from nano_notebooklm import config
    monkeypatch.setattr(config, "ARTIFACTS_DIR", art)
    return art


@pytest.fixture(autouse=True)
def disable_network():
    """Strip API keys so tests can't accidentally call out to LLM providers."""
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        os.environ.pop(key, None)
    yield


# fix-all v1 #B7 (R4-4 review-swarm): the FastAPI startup hook in
# api/server.py warms kb.embed_fn at boot. TestClient triggers it on every
# `with TestClient(server_mod.app)` enter, which would cost 3-10s per
# reload across the suite — multiplying pytest wall time without
# exercising any production behavior. Disable globally for tests; the
# hook is verified by tests/test_r4_4_fix_all_v1.py source-pin grep.
os.environ.setdefault("NANO_NLM_DISABLE_EMBED_WARMUP", "1")
