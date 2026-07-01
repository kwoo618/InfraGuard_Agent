"""
test_metrics.py — get_metrics Tool 단위 테스트.

get_metrics.py의 _query_prometheus 내부에서 httpx.AsyncClient를 생성한다.
테스트에서는 PROMETHEUS_URL을 가짜 URL로 바꾸고,
httpx.AsyncClient에 MockTransport를 끼워서 실제 네트워크 없이 응답을 흉내낸다.
"""

import pytest
import httpx
import importlib

import app.tools.get_metrics as gm_module
from app.schemas import SystemMetrics


# ── 헬퍼 ──────────────────────────────────────────────────────────────────────

def _prometheus_body(value: float) -> dict:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": {}, "value": [1700000000, str(value)]}],
        },
    }


def _empty_body() -> dict:
    return {"status": "success", "data": {"resultType": "vector", "result": []}}


# ── 테스트 1: 정상 응답 ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_system_metrics_normal(monkeypatch):
    """cpu=45.5, mem=60.0, connection=3 응답 → SystemMetrics 값이 맞는지."""
    responses = [
        _prometheus_body(45.5),  # cpu
        _prometheus_body(60.0),  # mem
        _prometheus_body(3.0),   # connection
    ]
    call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        body = responses[call_count[0] % len(responses)]
        call_count[0] += 1
        return httpx.Response(200, json=body)

    real_client_cls = httpx.AsyncClient

    def patched_client(**kwargs):
        return real_client_cls(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(gm_module.httpx, "AsyncClient", patched_client)

    result = await gm_module.get_system_metrics()

    assert isinstance(result, SystemMetrics)
    assert result.cpu_pct == 45.5
    assert result.mem_pct == 60.0
    assert result.connection_count == 3
    assert result.timestamp is not None


# ── 테스트 2: 빈 result 폴백 ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_system_metrics_empty_result(monkeypatch):
    """result=[] 응답 → 전부 0.0/0 폴백, 에러 없음."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_empty_body())

    real_client_cls = httpx.AsyncClient

    def patched_client(**kwargs):
        return real_client_cls(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(gm_module.httpx, "AsyncClient", patched_client)

    result = await gm_module.get_system_metrics()

    assert result.cpu_pct == 0.0
    assert result.mem_pct == 0.0
    assert result.connection_count == 0


# ── 테스트 3: 연결 실패 폴백 ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_system_metrics_connection_error(monkeypatch):
    """ConnectError 발생 → 에러로 죽지 않고 0.0 폴백."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    real_client_cls = httpx.AsyncClient

    def patched_client(**kwargs):
        return real_client_cls(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(gm_module.httpx, "AsyncClient", patched_client)

    result = await gm_module.get_system_metrics()

    assert result.cpu_pct == 0.0
    assert result.mem_pct == 0.0
    assert result.connection_count == 0