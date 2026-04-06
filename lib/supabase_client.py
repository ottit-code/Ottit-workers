import threading
from supabase import create_client, Client
from lib.config import SUPABASE_URL, SUPABASE_SERVICE_KEY

_client: Client | None = None
_lock = threading.Lock()

def get_supabase() -> Client:
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    return _client
