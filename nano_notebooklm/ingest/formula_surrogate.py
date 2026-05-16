"""LaTeX → natural-language surrogate text for formula-bearing chunks.

The MinerU extractor recovers equations as LaTeX (`$$\\sum_t \\alpha_t(i)\\beta_t(i)$$`),
which is great for the answer LLM but terrible for retrieval:

  - BM25's character-bigram tokenizer slices `\\sum` into `\\s/su/um/m_` — garbage.
  - MiniLM L6 embeddings rarely saw LaTeX in training, so `\\frac{1}{N}` lands
    in a random region of the embedding space far from the Chinese question
    "前向概率怎么算".

The fix: for every chunk where `has_formula=True`, ask an LLM to rewrite the
text so each `$$...$$` (and `$...$`) is replaced with a natural-language
description in the same language as the surrounding prose. Store this on
the chunk as `embed_text`. Retrieval (FAISS vector + BM25) uses
`embed_text or text`; the answer LLM still gets the original `text` with
full LaTeX intact.

Cost shape:
  - One small LLM call per formula-bearing chunk (~500 input + ~300 output tok)
  - Concurrency cap so a 100-chunk course doesn't fan out 100 simultaneous
    requests
  - Per-chunk timeout — on failure we leave `embed_text=None` so the index
    falls back to the raw text. Net effect: surrogate is purely additive,
    never worse than the no-surrogate baseline.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Awaitable, Callable

from nano_notebooklm.ai.router import ModelRouter
from nano_notebooklm.types import Chunk

logger = logging.getLogger(__name__)


SURROGATE_PROMPT = """你是一个数学公式翻译助手。把下面文本中的 LaTeX 公式翻译成自然语言描述，保留所有非公式文本完全不变。

规则：
1. 把 $$...$$ block 替换为 "公式：<自然语言描述>"，嵌入原位置
2. 把 inline $...$ 替换为 "<自然语言描述>"
3. 数学符号用中文术语：α/alpha → 阿尔法或前向变量、β → 贝塔或后向变量、∑ → 求和、∫ → 积分、∏ → 连乘、∂ → 偏导、δ → 维特比变量
4. 状态变量保留 q_t / s_j 这种容易搜的形式（不要写成"q下标t"）
5. 保留所有非公式中文 / 英文文本一字不改
6. 输出语言与输入一致

举例：
输入：维特比变量 δ_t(i) 满足递归 $$\\delta_t(j) = \\max_i [\\delta_{t-1}(i) \\cdot a_{ij}] \\cdot b_j(o_t)$$
输出：维特比变量 δ_t(i) 满足递归 公式：在时刻t状态j的维特比变量等于 在时刻t-1所有状态i上 维特比变量乘以从i到j的状态转移概率a_ij 取最大值 再乘以状态j在时刻t发射观测o_t的概率b_j(o_t)

仅输出改写后的文本，不要任何 markdown 标记、不要解释，不要前后缀。

输入：
{text}

输出："""


# A "formula-bearing" chunk that's worth surrogating: must actually have at
# least one $$...$$ block or one \LaTeX_macro. We don't surrogate a chunk
# that only has has_formula=True because of a stray bigram match — that's
# a chunker-side false-positive and would burn LLM budget on prose.
_NEEDS_SURROGATE = re.compile(
    r"\$\$|\\(?:frac|sum|int|prod|alpha|beta|gamma|delta|sigma|mu|pi|theta|lambda|cdot)\b"
)


def _is_worth_surrogating(text: str) -> bool:
    return bool(_NEEDS_SURROGATE.search(text))


async def annotate_chunks_with_surrogate(
    chunks: list[Chunk],
    router: ModelRouter,
    *,
    concurrency: int = 4,
    per_chunk_timeout_seconds: float = 30.0,
    progress_callback: Callable[[int, int], None] | None = None,
) -> list[Chunk]:
    """Mutate ``chunks`` in-place: every formula-bearing chunk gets
    ``embed_text`` set to an LLM-paraphrased version with LaTeX replaced
    by natural language.

    Chunks where the surrogate call fails (timeout / LLM error / empty
    response) keep ``embed_text=None`` so downstream search falls back
    to ``text``. This is intentionally additive — never net negative.

    Args:
      chunks: list to annotate. Non-formula chunks pass through unchanged.
      router: the ``ModelRouter`` instance to use for LLM calls.
      concurrency: max in-flight LLM calls. 4 is comfortable for codex.
      per_chunk_timeout_seconds: kill switch per LLM call; on timeout the
        chunk is left without a surrogate (logged warning).
      progress_callback: ``(done, total) -> None`` called after each chunk
        completes (success OR failure). Total only counts chunks we
        attempted, not the no-op pass-through ones.

    Returns the same chunk list (mutated).
    """
    targets = [c for c in chunks if c.has_formula and _is_worth_surrogating(c.text)]
    if not targets:
        logger.info("formula_surrogate: nothing to do (no formula-bearing chunks)")
        return chunks

    sem = asyncio.Semaphore(concurrency)
    total = len(targets)
    done_count = 0
    lock = asyncio.Lock()

    async def _run_one(chunk: Chunk) -> None:
        nonlocal done_count
        async with sem:
            try:
                resp = await asyncio.wait_for(
                    router.complete(
                        SURROGATE_PROMPT.format(text=chunk.text),
                        task_type="formula_surrogate",
                        temperature=0.2,
                        max_tokens=2048,
                        max_retries=1,
                    ),
                    timeout=per_chunk_timeout_seconds,
                )
                surrogate = (resp.content or "").strip()
                # Guard: an empty / collapsed response means the LLM gave
                # up — fall back to text.
                if surrogate and len(surrogate) >= 0.3 * len(chunk.text):
                    chunk.embed_text = surrogate
                else:
                    logger.warning(
                        "formula_surrogate: response too short for %s "
                        "(got %d chars vs source %d)",
                        chunk.chunk_id, len(surrogate), len(chunk.text),
                    )
            except asyncio.TimeoutError:
                logger.warning("formula_surrogate: timeout on %s", chunk.chunk_id)
            except Exception as exc:
                logger.warning(
                    "formula_surrogate: failed on %s: %s",
                    chunk.chunk_id, type(exc).__name__,
                )
            finally:
                async with lock:
                    done_count += 1
                    if progress_callback:
                        try:
                            progress_callback(done_count, total)
                        except Exception:
                            pass

    await asyncio.gather(*(_run_one(c) for c in targets))
    annotated = sum(1 for c in targets if c.embed_text)
    logger.info(
        "formula_surrogate: %d/%d chunks annotated (%.0f%% success)",
        annotated, total, 100.0 * annotated / total,
    )
    return chunks
