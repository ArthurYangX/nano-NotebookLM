"""Local-model backend.

Any OpenAI-compatible /v1 server works: Ollama, vLLM, LM Studio,
llama.cpp server, TGI, Together-on-prem, etc. Configured via
LOCAL_LLM_BASE_URL / LOCAL_LLM_MODEL / LOCAL_LLM_API_KEY.

This is a thin subclass of OpenAIBackend so cloud + local can coexist
as two independent backends (frontend chip switches between them).
"""

from __future__ import annotations

from nano_notebooklm import config
from nano_notebooklm.ai.openai_backend import OpenAIBackend


class LocalBackend(OpenAIBackend):
    name = "local"

    def __init__(self):
        super().__init__(
            api_key=config.LOCAL_LLM_API_KEY or "local",
            base_url=config.LOCAL_LLM_BASE_URL,
            model=config.LOCAL_LLM_MODEL,
        )
