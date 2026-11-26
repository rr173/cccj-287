#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
export DOC_REVIEW_DB="${DOC_REVIEW_DB:-./data/review.db}"
export PORT="${PORT:-8000}"
export HOST="${HOST:-127.0.0.1}"

exec python3 -m uvicorn doc_review.main:app --host "$HOST" --port "$PORT"
