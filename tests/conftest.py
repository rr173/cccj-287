import os

# Avoid creating ./data/review.db merely because pytest imports the ASGI app.
# Individual tests still call create_app() with their own temporary file.
os.environ.setdefault("DOC_REVIEW_DB", ":memory:")
