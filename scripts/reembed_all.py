"""One-shot reembed: rebuild FAISS+BM25 + KG concept_embeddings under the
current EMBEDDING_MODE (api / local). Run after switching embedding model
so vectors stay dimension-consistent.

Usage:  PYTHONUNBUFFERED=1 python scripts/reembed_all.py
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

# allow `python scripts/reembed_all.py` from project root
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> None:
    from nano_notebooklm import config
    log(f"EMBEDDING_MODE={config.EMBEDDING_MODE} EMBEDDING_MODEL={config.EMBEDDING_MODEL}")

    from nano_notebooklm.kb.store import KBStore
    from nano_notebooklm.kb.graph_search import _concept_embed_text, _load_chunks_index

    # ── 1. Strip stale KG concept_embeddings (dim may have changed) ──
    artifacts = Path("artifacts")
    log("Stripping stale KG concept_embeddings…")
    for kg_path in artifacts.glob("courses/*/knowledge_graph.json"):
        kg = json.loads(kg_path.read_text())
        stripped = 0
        for n in kg.get("nodes") or []:
            if "concept_embedding" in n:
                del n["concept_embedding"]
                stripped += 1
        if stripped:
            tmp = kg_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(kg, ensure_ascii=False, indent=2))
            tmp.rename(kg_path)
            log(f"  · {kg_path.parent.name}: stripped {stripped}")

    # ── 2. Rebuild FAISS + BM25 with batched progress ──
    kb = KBStore()
    log("Loading all chunks for global index rebuild…")
    chunks = kb._load_all_chunks(None)
    log(f"  · loaded {len(chunks)} chunks")

    if not chunks:
        log("nothing to index; aborting")
        return

    # Embed in batches with explicit progress instead of going through
    # VectorIndex.build's silent loop (we want to see API call latency).
    import numpy as np
    import faiss
    from nano_notebooklm.kb.bm25_index import BM25Index
    from nano_notebooklm.kb.vector_index import VectorIndex
    from nano_notebooklm.kb.hybrid_search import HybridSearch

    embed_fn = kb.embed_fn
    texts = [c.text for c in chunks]
    # R5 step C (2026-05-16): the default _build_api_embed_fn in
    # kb/store.py is fully serial — one HTTP request after another, no
    # concurrency. With text-embedding-3-large on the codex proxy each
    # 64-input request takes 60-120s, so 10k chunks at inner batch=64
    # serial = 3.3 hours. We bypass it here and drive the OpenAI client
    # directly with a ThreadPoolExecutor + larger inner batches. The
    # main code path is untouched (ingest still goes through embed_fn
    # the normal way; only this one-shot rebuild uses the parallel path).
    import openai
    from concurrent.futures import ThreadPoolExecutor, as_completed

    client = openai.OpenAI(api_key=config.OPENAI_API_KEY, base_url=config.OPENAI_BASE_URL)
    batch_size = 256          # OpenAI limit: 2048 inputs per call. 256 keeps
                              # per-call body small enough that codex proxy
                              # rarely OOMs / times out.
    concurrency = 8           # 8 parallel inflight requests. codex proxy
                              # has been tested up to 8 without 429s.
    n_batches = (len(texts) + batch_size - 1) // batch_size
    log(f"Embedding {len(texts)} chunks in {n_batches} batches of {batch_size} "
        f"(concurrency={concurrency})…")

    def _embed_one(idx_and_batch):
        idx, batch = idx_and_batch
        t_start = time.time()
        resp = client.embeddings.create(model=config.EMBEDDING_MODEL, input=batch)
        arr = np.asarray([d.embedding for d in resp.data], dtype=np.float32)
        # L2-normalize so FAISS inner-product == cosine
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return idx, arr / norms, time.time() - t_start

    batches = [(i // batch_size, texts[i : i + batch_size])
               for i in range(0, len(texts), batch_size)]
    results: dict[int, np.ndarray] = {}
    t0 = time.time()
    completed = 0
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        for idx, arr, dur in (
            f.result() for f in as_completed(ex.submit(_embed_one, b) for b in batches)
        ):
            results[idx] = arr
            completed += 1
            chunks_done = completed * batch_size
            elapsed = time.time() - t0
            log(f"  · batch {idx+1}/{n_batches} ({dur:.1f}s req) · "
                f"elapsed {elapsed:.1f}s · {chunks_done/elapsed:.0f} chunks/s")
    embeddings = np.vstack([results[i] for i in range(n_batches)])
    log(f"Embeddings ready: {embeddings.shape} in {time.time()-t0:.1f}s")

    vector = VectorIndex(embed_fn)
    vector.chunks = list(chunks)
    vector._dim = embeddings.shape[1]
    vector.index = faiss.IndexFlatIP(vector._dim)
    vector.index.add(embeddings)
    log(f"FAISS IndexFlatIP built (dim={vector._dim})")

    bm25 = BM25Index()
    bm25.build(chunks)
    log("BM25 built")

    index_dir = artifacts / "indices"
    vector.save(index_dir / "faiss" / "global")
    bm25.save(index_dir / "bm25" / "global.json")
    log(f"Saved global indices under {index_dir}/")

    # ── 3. Re-bake KG concept_embeddings via enriched text ──
    log("Re-baking KG concept_embeddings…")
    for kg_path in artifacts.glob("courses/*/knowledge_graph.json"):
        course = kg_path.parent.name
        kg = json.loads(kg_path.read_text())
        chunks_idx = _load_chunks_index(course, artifacts)
        chunk_text_lookup = {cid: row.get("text", "") for cid, row in chunks_idx.items()}
        non_root = [n for n in kg["nodes"] if (n.get("concept_type") or "").lower() != "root"]
        targets = [n for n in non_root if not n.get("concept_embedding")]
        if not targets:
            log(f"  · {course}: skip (all cached)")
            continue
        node_texts = [_concept_embed_text(n, chunk_text_lookup) for n in targets]
        t1 = time.time()
        out = embed_fn(node_texts)
        for n, e in zip(targets, out):
            n["concept_embedding"] = [float(x) for x in e]
        tmp = kg_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(kg, ensure_ascii=False, indent=2))
        tmp.rename(kg_path)
        log(f"  · {course}: baked {len(targets)} concepts in {time.time()-t1:.1f}s")

    log("ALL DONE")


if __name__ == "__main__":
    main()
