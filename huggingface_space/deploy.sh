#!/usr/bin/env bash
# Deploy nano-NotebookLM to a HuggingFace Space.
#
# Usage:
#   ./huggingface_space/deploy.sh <hf-username/space-name>
#
# Prereqs (one-time): see ./huggingface_space/DEPLOY.md

set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <hf-username/space-name>" >&2
  echo "see huggingface_space/DEPLOY.md for one-time setup" >&2
  exit 1
fi

SPACE="$1"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGING="$(mktemp -d)"

trap 'rm -rf "$STAGING"' EXIT

echo "→ Staging clean tree at $STAGING"

# rsync the working tree, excluding everything HF doesn't need.
# We deliberately do NOT push tests, sample PDFs, screenshots, the
# venv, or any local artifacts.
rsync -a \
  --exclude='.git/' \
  --exclude='.venv/' \
  --exclude='venv/' \
  --exclude='artifacts/' \
  --exclude='output/' \
  --exclude='dist/' \
  --exclude='build/' \
  --exclude='.pytest_cache/' \
  --exclude='__pycache__/' \
  --exclude='*.egg-info/' \
  --exclude='tests/' \
  --exclude='.playwright-mcp/' \
  --exclude='.claude/' \
  --exclude='.github/' \
  --exclude='.idea/' \
  --exclude='.vscode/' \
  --exclude='pics/' \
  --exclude='docs/' \
  --exclude='*.pyc' \
  --exclude='.DS_Store' \
  --exclude='截屏*.png' \
  --exclude='Screenshot*.png' \
  --exclude='test-*.pdf' \
  --exclude='sample-*.pdf' \
  --exclude='ppt.pdf' \
  "$ROOT/" "$STAGING/"

# Swap in the Space-flavoured README (the one with YAML frontmatter
# HF Spaces requires for sdk:docker + app_port).
cp "$ROOT/huggingface_space/README.md" "$STAGING/README.md"

# The huggingface_space/ dir itself isn't useful inside the deployed
# Space — drop it.
rm -rf "$STAGING/huggingface_space"

cd "$STAGING"
git init -q -b main
git add -A
git commit -q -m "Deploy nano-NotebookLM to HuggingFace Spaces"
git remote add origin "https://huggingface.co/spaces/$SPACE"

echo "→ Force-pushing to https://huggingface.co/spaces/$SPACE"
git push -f origin main

echo
echo "✓ Pushed. Watch the build at:"
echo "  https://huggingface.co/spaces/$SPACE"
echo
echo "First build takes ~5-10 min. When the Building badge turns green,"
echo "the Space is live."
