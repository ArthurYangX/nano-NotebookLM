"""Incremental update detection via file hashing."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from nano_notebooklm.utils.file_hash import sha256_file


@dataclass
class ChangeSet:
    added: list[Path] = field(default_factory=list)
    modified: list[Path] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)  # relative paths

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.modified or self.deleted)


def detect_changes(
    files: list[Path],
    base_dir: Path,
    hash_cache_path: Path,
) -> ChangeSet:
    """Compare current files against cached hashes to find changes.

    Returns a ChangeSet with added, modified, and deleted files.
    """
    # Load existing hash cache
    old_hashes: dict[str, str] = {}
    if hash_cache_path.exists():
        old_hashes = json.loads(hash_cache_path.read_text())

    current_hashes: dict[str, str] = {}
    changeset = ChangeSet()

    for filepath in files:
        rel = str(filepath.relative_to(base_dir))
        file_hash = sha256_file(filepath)
        current_hashes[rel] = file_hash

        if rel not in old_hashes:
            changeset.added.append(filepath)
        elif old_hashes[rel] != file_hash:
            changeset.modified.append(filepath)

    # Detect deleted files
    for rel in old_hashes:
        if rel not in current_hashes:
            changeset.deleted.append(rel)

    return changeset


def save_hashes(files: list[Path], base_dir: Path, hash_cache_path: Path):
    """Save current file hashes to cache."""
    hashes = {}
    for filepath in files:
        rel = str(filepath.relative_to(base_dir))
        hashes[rel] = sha256_file(filepath)
    hash_cache_path.parent.mkdir(parents=True, exist_ok=True)
    hash_cache_path.write_text(json.dumps(hashes, indent=2))
