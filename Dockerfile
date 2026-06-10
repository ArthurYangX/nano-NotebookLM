# nano-NotebookLM — slim runtime image (~300 MB)
# Default extraction engine: pymupdf. MinerU OCR is NOT bundled here —
# it ships ~3 GB of torch + models and would push the image past 4 GB.
# To enable MinerU, build with: docker build --build-arg WITH_MINERU=1 .

FROM python:3.11-slim-bookworm

ARG WITH_MINERU=0

# libgomp1 is faiss' OpenMP runtime; curl is for the HEALTHCHECK.
# Modern manylinux wheels cover arm64 + x86_64 for every pinned dep,
# so we deliberately skip build-essential to keep the image slim.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy package metadata + source. We don't split deps into a separate
# layer because pyproject.toml's editable install needs the package
# directory present at install time; the marginal cache benefit isn't
# worth the maintenance cost of a parallel requirements.txt.
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY nano_notebooklm/ ./nano_notebooklm/
COPY api/ ./api/
COPY frontend/ ./frontend/
COPY scripts/ ./scripts/

# Install CPU-only torch FIRST so the default sentence-transformers
# dep doesn't drag in ~5 GB of CUDA libraries. Order matters here.
RUN pip install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cpu \
        torch

RUN pip install --no-cache-dir -e . && \
    if [ "$WITH_MINERU" = "1" ]; then pip install --no-cache-dir -e ".[mineru]"; fi

RUN mkdir -p /app/artifacts /app/output

ENV PYTHONUNBUFFERED=1 \
    NANO_NLM_PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -sf http://127.0.0.1:8000/api/health || exit 1

CMD ["python", "api/server.py"]
