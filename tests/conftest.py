"""
Shared test fixtures for all poller tests.
"""
from unittest.mock import MagicMock
import pytest


@pytest.fixture
def mock_supabase():
    """Return a MagicMock that mimics the Supabase client's chained query interface."""
    sb = MagicMock()
    # Default: all table operations return empty data
    sb.table.return_value.select.return_value.eq.return_value.in_.return_value \
        .execute.return_value.data = []
    sb.table.return_value.select.return_value.eq.return_value \
        .limit.return_value.execute.return_value.data = []
    sb.table.return_value.upsert.return_value.execute.return_value.data = []
    sb.rpc.return_value.execute.return_value.data = None
    return sb


@pytest.fixture
def campaign_ids():
    return ["camp1", "camp2"]
