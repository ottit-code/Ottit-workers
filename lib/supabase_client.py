import threading

from supabase import create_client, Client
from supabase.client import ClientOptions

from lib.config import SUPABASE_URL, SUPABASE_SERVICE_KEY

# All worker tables, RPCs, and views live in the `v1` schema (the cutover
# from `public` happened in the Supabase project itself). Setting the schema
# globally on the client makes every `.table(...)` and `.rpc(...)` call
# resolve to `v1.*` automatically — no per-call rewrites required.
#
# Storage operations are NOT affected by this option (Storage has no notion
# of Postgres schemas), so `voice-assets/...` paths keep working.
_OPTIONS = ClientOptions(schema="v1")

_client: Client | None = None
_lock = threading.Lock()


def get_supabase() -> Client:
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = create_client(
                    SUPABASE_URL,
                    SUPABASE_SERVICE_KEY,
                    options=_OPTIONS,
                )
    return _client
