"""Tests for src.clients.supabase_client.SupabaseClient — httpx mocked at wrapper boundary."""

from __future__ import annotations

from unittest.mock import MagicMock

from src.clients.supabase_client import SupabaseClient


def _fake_response(payload: object) -> MagicMock:
    r = MagicMock(name="Response")
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


async def test_select_sends_correct_url_and_params(mock_httpx_client):
    mock_httpx_client.get.return_value = _fake_response([{"id": 1, "symbol": "MNQM6"}])

    client = SupabaseClient(url="https://x.supabase.co", key="k", http_client=mock_httpx_client)
    result = await client.select("trades", filters={"symbol": "eq.MNQM6"})

    mock_httpx_client.get.assert_awaited_once()
    call_url = mock_httpx_client.get.call_args.args[0]
    assert call_url == "https://x.supabase.co/rest/v1/trades"
    params = mock_httpx_client.get.call_args.kwargs["params"]
    assert params == {"select": "*", "symbol": "eq.MNQM6"}
    assert result == [{"id": 1, "symbol": "MNQM6"}]


async def test_select_without_filters(mock_httpx_client):
    mock_httpx_client.get.return_value = _fake_response([])

    client = SupabaseClient(url="https://x.supabase.co", key="k", http_client=mock_httpx_client)
    result = await client.select("trades", columns="id,symbol")

    params = mock_httpx_client.get.call_args.kwargs["params"]
    assert params == {"select": "id,symbol"}
    assert result == []


async def test_select_strips_trailing_slash_on_base_url(mock_httpx_client):
    mock_httpx_client.get.return_value = _fake_response([])

    client = SupabaseClient(url="https://x.supabase.co/", key="k", http_client=mock_httpx_client)
    await client.select("trades")

    call_url = mock_httpx_client.get.call_args.args[0]
    assert call_url == "https://x.supabase.co/rest/v1/trades"


async def test_insert_single_dict(mock_httpx_client):
    mock_httpx_client.post.return_value = _fake_response([{"id": 1}])

    client = SupabaseClient(url="https://x.supabase.co", key="k", http_client=mock_httpx_client)
    result = await client.insert("trades", {"symbol": "MNQM6", "qty": 2})

    mock_httpx_client.post.assert_awaited_once()
    call_url = mock_httpx_client.post.call_args.args[0]
    assert call_url == "https://x.supabase.co/rest/v1/trades"
    assert mock_httpx_client.post.call_args.kwargs["json"] == {"symbol": "MNQM6", "qty": 2}
    assert mock_httpx_client.post.call_args.kwargs["headers"] == {"Prefer": "return=representation"}
    assert result == [{"id": 1}]


async def test_insert_list_of_dicts(mock_httpx_client):
    mock_httpx_client.post.return_value = _fake_response([{"id": 1}, {"id": 2}])
    payload = [{"symbol": "MNQM6"}, {"symbol": "MNQU6"}]

    client = SupabaseClient(url="https://x.supabase.co", key="k", http_client=mock_httpx_client)
    result = await client.insert("trades", payload)

    assert mock_httpx_client.post.call_args.kwargs["json"] == payload
    assert result == [{"id": 1}, {"id": 2}]


async def test_upsert_with_on_conflict(mock_httpx_client):
    mock_httpx_client.post.return_value = _fake_response([{"id": 1}])

    client = SupabaseClient(url="https://x.supabase.co", key="k", http_client=mock_httpx_client)
    await client.upsert("trades", {"trade_id": "abc", "qty": 2}, on_conflict="trade_id")

    kwargs = mock_httpx_client.post.call_args.kwargs
    assert kwargs["params"] == {"on_conflict": "trade_id"}
    assert kwargs["headers"] == {"Prefer": "resolution=merge-duplicates,return=representation"}
    assert kwargs["json"] == {"trade_id": "abc", "qty": 2}


async def test_upsert_without_on_conflict(mock_httpx_client):
    mock_httpx_client.post.return_value = _fake_response([{"id": 1}])

    client = SupabaseClient(url="https://x.supabase.co", key="k", http_client=mock_httpx_client)
    await client.upsert("trades", [{"trade_id": "abc"}])

    assert mock_httpx_client.post.call_args.kwargs["params"] == {}


async def test_close_calls_aclose(mock_httpx_client):
    client = SupabaseClient(url="https://x.supabase.co", key="k", http_client=mock_httpx_client)
    await client.close()
    mock_httpx_client.aclose.assert_awaited_once()
