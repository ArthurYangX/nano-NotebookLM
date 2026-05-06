"""File hashing for incremental update detection."""

import hashlib
from pathlib import Path


def sha256_file(filepath: str | Path) -> str:
    """Compute SHA256 hash of a file's contents."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for block in iter(lambda: f.read(8192), b""):
            h.update(block)
    return h.hexdigest()
