"""
test_metrics.py — get_metrics Tool 단위 테스트.

윈도우 Docker Desktop 환경에서 cAdvisor name 라벨 미지원으로
cpu_pct / mem_pct는 0.0 고정 반환한다.
connection_count만 Prometheus 쿼리로 가져온다.

테스트 3가지:
  1. 정상 응답 → connection_count에 숫자가 들어오는지, cpu/mem은 0.0인지
  2. 빈 result → connection_count 0 폴백
  3. 연결 실패 → 에러로 죽지 않고 0 폴백
"""

import pytest
import httpx

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
    """
    Prometheus가 connection=3 을 응답했을 때
    - connection_count == 3
    - cpu_pct == 0.0  (cAdvisor 미지원으로 고정값)
    - mem_pct == 0.0  (cAdvisor 미지원으로 고정값)
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_prometheus_body(3.0))

    real_client_cls = httpx.AsyncClient

    def patched_client(**kwargs):
        return real_client_cls(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(gm_module.httpx, "AsyncClient", patched_client)

    result = await gm_module.get_system_metrics()

    assert isinstance(result, SystemMetrics)
    assert result.connection_count == 3
    assert result.cpu_pct == 0.0      # 윈도우 환경 cAdvisor 미지원 고정값
    assert result.mem_pct == 0.0      # 윈도우 환경 cAdvisor 미지원 고정값
    assert result.timestamp is not None


# ── 테스트 2: 빈 result 폴백 ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_system_metrics_empty_result(monkeypatch):
    """result=[] 응답 → connection_count 0 폴백, 에러 없음."""
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
    """ConnectError 발생 → 에러로 죽지 않고 0 폴백."""
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