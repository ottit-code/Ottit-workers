"""
webhook_receiver.py — now delegates to api/main.py

Run the full API server with:
  uvicorn api.main:app --host 0.0.0.0 --port 8000
"""

from api.main import app  # re-export for backwards compatibility
