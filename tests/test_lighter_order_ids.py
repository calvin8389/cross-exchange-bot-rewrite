from __future__ import annotations

import pytest

from src.exchanges.lighter_adapter import LighterAdapter


@pytest.mark.asyncio
async def test_lighter_client_order_ids_are_unique_and_monotonic():
    adapter = LighterAdapter(
        ws_url="wss://example.invalid/stream",
        rest_url="https://example.invalid",
        account_index=0,
    )
    try:
        ids = [await adapter.next_client_order_id() for _ in range(500)]
        assert len(ids) == len(set(ids))
        assert ids == sorted(ids)
    finally:
        await adapter.close()
