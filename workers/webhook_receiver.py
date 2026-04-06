"""
webhook_receiver.py — always-on FastAPI server for EmailBison webhooks (implemented in Phase 5)
"""
from fastapi import FastAPI

app = FastAPI(title="Ottit Webhook Receiver")

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/webhook")
async def receive_webhook(payload: dict):
    # Phase 5 will implement full webhook handling
    return {"received": True}
