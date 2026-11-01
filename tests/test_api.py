"""Tests for the Geniki Taxydromiki API client."""

import json
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.geniki_taxydromiki import api
from custom_components.geniki_taxydromiki.api import (
    GenikiTaxydromikiApiClient,
    GenikiTaxydromikiApiError,
)
from custom_components.geniki_taxydromiki.const import TRACKING_API_URL

from .payloads import (
    ACTIVE_CODE,
    UNKNOWN_CODE,
    active_sample,
    envelope,
    not_found_envelope,
)


@pytest.fixture(autouse=True)
def _reset_warnings():
    api._warned.clear()


def _session_returning(
    status: int, body: object = None, headers: dict | None = None
) -> MagicMock:
    response = AsyncMock()
    response.status = status
    response.headers = headers or {}
    if isinstance(body, str):
        response.json = AsyncMock(side_effect=json.JSONDecodeError("x", body, 0))
    else:
        response.json = AsyncMock(return_value=body)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.post = MagicMock(return_value=ctx)
    return session


async def test_get_parcel_posts_the_bare_json_string_as_a_form_body():
    session = _session_returning(200, envelope(ACTIVE_CODE, active_sample()))
    await GenikiTaxydromikiApiClient(session).async_get_parcel(ACTIVE_CODE)

    args, kwargs = session.post.call_args
    assert args == (TRACKING_API_URL,)
    assert kwargs["data"] == '{"lang":"en","voucherNo":"1000000001"}'
    assert kwargs["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert kwargs["headers"]["Accept"] == "application/json"
    assert kwargs["timeout"].total == 60


async def test_get_parcel_returns_the_result_object_complete():
    inner = active_sample()
    client = GenikiTaxydromikiApiClient(
        _session_returning(200, envelope(ACTIVE_CODE, inner))
    )
    assert await client.async_get_parcel(ACTIVE_CODE) is inner


async def test_result_9_is_not_found_despite_http_200():
    client = GenikiTaxydromikiApiClient(_session_returning(200, not_found_envelope()))
    assert await client.async_get_parcel(UNKNOWN_CODE) is None


@pytest.mark.parametrize("result", [10, 11, "0", None])
async def test_unexpected_result_raises_and_warns_once(result, caplog):
    inner = {"Result": result, "Checkpoints": {}}
    session = _session_returning(200, envelope(ACTIVE_CODE, inner))
    client = GenikiTaxydromikiApiClient(session)
    for _ in range(2):
        with pytest.raises(GenikiTaxydromikiApiError):
            await client.async_get_parcel(ACTIVE_CODE)
    assert caplog.text.count("unexpected Result") == 1
    assert "issues/new" in caplog.text


@pytest.mark.parametrize(
    "inner",
    [
        {"Result": 0, "Checkpoints": {}},
        {"Result": 0, "Checkpoints": {"Checkpoint": []}},
        {"Result": 0, "Checkpoints": {"Checkpoint": None}},
        {"Result": 0, "Checkpoints": []},
        {"Result": 0, "Checkpoints": None},
        {"Result": 0, "Checkpoints": ""},
        {"Result": 0},
    ],
)
async def test_found_code_without_checkpoints_reads_as_unknown_and_warns(inner, caplog):
    client = GenikiTaxydromikiApiClient(
        _session_returning(200, envelope(ACTIVE_CODE, inner))
    )
    assert await client.async_get_parcel(ACTIVE_CODE) is None
    assert "without checkpoints" in caplog.text
    assert "issues/new" in caplog.text


async def test_timeout_raises_the_api_error():
    session = MagicMock()
    session.post.return_value.__aenter__ = AsyncMock(side_effect=TimeoutError)
    session.post.return_value.__aexit__ = AsyncMock(return_value=False)
    client = GenikiTaxydromikiApiClient(session)
    with pytest.raises(GenikiTaxydromikiApiError):
        await client.async_get_parcel(ACTIVE_CODE)


async def test_success_false_raises():
    client = GenikiTaxydromikiApiClient(_session_returning(200, {"success": False}))
    with pytest.raises(GenikiTaxydromikiApiError):
        await client.async_get_parcel(ACTIVE_CODE)


async def test_reply_not_keyed_by_the_code_raises_and_warns(caplog):
    client = GenikiTaxydromikiApiClient(
        _session_returning(200, envelope("2000000000", active_sample()))
    )
    with pytest.raises(GenikiTaxydromikiApiError):
        await client.async_get_parcel(ACTIVE_CODE)
    assert "2000000000" not in caplog.text
    assert "not keyed by the requested code" in caplog.text
    assert ACTIVE_CODE not in caplog.text


async def test_entry_without_result_object_raises_and_warns(caplog):
    client = GenikiTaxydromikiApiClient(
        _session_returning(200, {"success": True, ACTIVE_CODE: {"Other": 1}})
    )
    with pytest.raises(GenikiTaxydromikiApiError):
        await client.async_get_parcel(ACTIVE_CODE)
    assert "no TrackAndTraceResult" in caplog.text


@pytest.mark.parametrize("status", [500, 503, 403])
async def test_error_status_raises_with_the_code(status):
    client = GenikiTaxydromikiApiClient(_session_returning(status, {}))
    with pytest.raises(GenikiTaxydromikiApiError) as err:
        await client.async_get_parcel(ACTIVE_CODE)
    assert err.value.status_code == status


async def test_429_carries_retry_after_seconds():
    client = GenikiTaxydromikiApiClient(
        _session_returning(429, {}, {"Retry-After": "120"})
    )
    with pytest.raises(GenikiTaxydromikiApiError) as err:
        await client.async_get_parcel(ACTIVE_CODE)
    assert err.value.status_code == 429
    assert err.value.retry_after == 120.0


@pytest.mark.parametrize(
    "header", [{}, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}]
)
async def test_429_without_usable_retry_after(header):
    client = GenikiTaxydromikiApiClient(_session_returning(429, {}, header))
    with pytest.raises(GenikiTaxydromikiApiError) as err:
        await client.async_get_parcel(ACTIVE_CODE)
    assert err.value.retry_after is None


async def test_unparseable_body_raises():
    client = GenikiTaxydromikiApiClient(_session_returning(200, "<html>"))
    with pytest.raises(GenikiTaxydromikiApiError):
        await client.async_get_parcel(ACTIVE_CODE)


async def test_non_object_body_raises():
    client = GenikiTaxydromikiApiClient(_session_returning(200, ["not", "a", "dict"]))
    with pytest.raises(GenikiTaxydromikiApiError):
        await client.async_get_parcel(ACTIVE_CODE)


async def test_network_error_propagates():
    """ClientError is left alone — DataUpdateCoordinator already wraps it."""
    session = MagicMock()
    session.post = MagicMock(side_effect=aiohttp.ClientError("boom"))
    with pytest.raises(aiohttp.ClientError):
        await GenikiTaxydromikiApiClient(session).async_get_parcel(ACTIVE_CODE)
