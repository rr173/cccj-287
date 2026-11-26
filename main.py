"""Convenience entry point: `python3 main.py`."""

from __future__ import annotations

import os

import uvicorn

from doc_review.main import app

if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )
