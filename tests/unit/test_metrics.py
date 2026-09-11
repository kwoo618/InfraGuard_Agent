"""
test_metrics.py — get_metrics Tool 단위 테스트.

윈도우 Docker Desktop 환경에서 cAdvisor name 라벨 미지원으로
cpu_pct / mem_pct는 0.0 고정 반환한다.
connection_count만 Prometheus 쿼리로 가져온다.

테스트 4가지:
  1. 정상 응답 → connection_count에 숫자가 들어오는지, cpu/mem은 0.0인지
  2. 빈 result → connection_count 0 폴백
  3. 연결 실패 → 에러로 죽지 않고 0 폴백
  4. cpu/mem 고정값인 동안 RESOURCE_METRICS_COLLECTED가 False인지
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


# ── 테스트 4: 미수집 플래그 가드 ──────────────────────────────────────────────

def test_resource_metrics_flag_false_while_values_fixed():
    """
    cpu_pct / mem_pct가 0.0 고정값인 동안에는 RESOURCE_METRICS_COLLECTED가 False여야 한다.
    True면 프롬프트가 0.0을 측정값처럼 LLM에 넘긴다 (docs/02 ISSUE-10).
    cAdvisor 연동으로 실제 값을 채우는 변경에서 이 테스트도 함께 바꾼다.
    """
    assert gm_module.RESOURCE_METRICS_COLLECTED is False


# ── 서버별 요청 분산 (결과 패널 그래프, docs/03 Phase 4) ──────────────────────
# 아래 카운터 값은 테스트 입력값이며 측정값이 아니다.

def _patch_client(monkeypatch, handler):
    real_client_cls = httpx.AsyncClient

    def patched_client(**kwargs):
        return real_client_cls(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(gm_module.httpx, "AsyncClient", patched_client)


@pytest.mark.asyncio
async def test_get_request_counts_by_instance(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = request.url.params["query"]
        return httpx.Response(200, json={
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {"metric": {"instance": "172.18.0.3:8080"}, "value": [1700000000, "420"]},
                    {"metric": {"instance": "172.18.0.6:8080"}, "value": [1700000000, "15"]},
                ],
            },
        })

    _patch_client(monkeypatch, handler)

    counts = await gm_module.get_request_counts_by_instance()

    assert counts == {"172.18.0.3:8080": 420.0, "172.18.0.6:8080": 15.0}
    # 서버별로 합산하고, /health(헬스체크)와 /metrics(Prometheus 수집)는 세지 않는다
    assert "sum by (instance)" in seen["query"]
    assert 'handler=~"/light|/heavy|/flaky"' in seen["query"]


@pytest.mark.asyncio
async def test_get_request_counts_by_instance_http_error_is_none(monkeypatch):
    _patch_client(monkeypatch, lambda request: httpx.Response(500))

    assert await gm_module.get_request_counts_by_instance() is None


@pytest.mark.asyncio
async def test_get_request_counts_by_instance_connection_error_is_none(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    _patch_client(monkeypatch, handler)

    assert await gm_module.get_request_counts_by_instance() is None


@pytest.mark.asyncio
async def test_get_request_counts_by_instance_malformed_is_none(monkeypatch):
    body = {"data": {"result": [{"metric": {}, "value": [1700000000, "x"]}]}}
    _patch_client(monkeypatch, lambda request: httpx.Response(200, json=body))

    assert await gm_module.get_request_counts_by_instance() is None


def test_instance_request_deltas():
    before = {"172.18.0.3:8080": 1000.0}
    # 스케일로 새로 뜬 인스턴스(.6)는 부하 전 조회에 없다 → 0에서 시작
    after = {"172.18.0.3:8080": 1431.0, "172.18.0.6:8080": 428.0}

    assert gm_module.instance_request_deltas(before, after) == {
        "172.18.0.3:8080": 431,
        "172.18.0.6:8080": 428,
    }


def test_instance_request_deltas_counter_reset_is_null():
    # 재시작으로 카운터가 초기화되면 차이가 음수 → 그 인스턴스는 측정값 없음
    assert gm_module.instance_request_deltas({"a:8080": 500.0}, {"a:8080": 20.0}) == {"a:8080": None}


def test_instance_request_deltas_missing_snapshot_is_null():
    assert gm_module.instance_request_deltas(None, {"a:8080": 1.0}) is None
    assert gm_module.instance_request_deltas({"a:8080": 1.0}, None) is None
