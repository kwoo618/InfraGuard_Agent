"""
get_metrics — Prometheus에 현재 시스템 상태를 물어보고 SystemMetrics로 포장하는 Tool.

담당: 최강우
소비자: 박정기 (agent/nodes.py) — 이 함수가 반환한 SystemMetrics를 LLM이 읽고 진단함.

동작 순서:
1. Prometheus HTTP API(/api/v1/query)에 PromQL 쿼리를 날린다.
2. 요청 처리량(TPS), P95 응답시간, 활성 connection 수를 각각 쿼리한다.
3. 응답 JSON에서 숫자만 뽑아 SystemMetrics(schemas.py)로 포장해 반환한다.

주의:
- 윈도우 Docker Desktop 환경에서 cAdvisor가 name 라벨을 붙이지 않아
  컨테이너별 CPU/메모리 필터링이 불가능하다.
- 대신 target-server가 /metrics로 직접 노출하는 애플리케이션 레벨 메트릭을 사용한다.
  (prometheus-fastapi-instrumentator 제공)
- cpu_pct, mem_pct는 0.0으로 고정 반환한다. 향후 리눅스 환경에서 cAdvisor 연동 시 교체.
- Prometheus가 아직 데이터를 못 모았거나 쿼리 결과가 비어있으면
  0.0 / 0으로 안전하게 기본값 처리한다 (예외로 죽이지 않는다).
"""

import os

import httpx

from app.schemas import SystemMetrics

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
QUERY_TIMEOUT_SECONDS = 5.0


async def _query_prometheus(promql: str) -> float:
    """
    Prometheus /api/v1/query에 단일 쿼리를 날리고 첫 번째 결과값을 float로 반환한다.
    결과가 없거나 요청이 실패하면 0.0을 반환한다 (에이전트 루프를 막지 않기 위함).
    """
    url = f"{PROMETHEUS_URL}/api/v1/query"

    try:
        async with httpx.AsyncClient(timeout=QUERY_TIMEOUT_SECONDS) as client:
            response = await client.get(url, params={"query": promql})
    except httpx.HTTPError:
        return 0.0

    if response.status_code != 200:
        return 0.0

    data = response.json()
    result = data.get("data", {}).get("result", [])

    if not result:
        return 0.0

    # value = [timestamp, "측정값(문자열)"]
    _, raw_value = result[0]["value"]

    try:
        return float(raw_value)
    except (TypeError, ValueError):
        return 0.0


async def get_system_metrics() -> SystemMetrics:
    """
    target-server의 애플리케이션 레벨 메트릭을 Prometheus에서 조회해 SystemMetrics로 반환한다.

    cpu_pct / mem_pct: 윈도우 Docker Desktop 환경에서 cAdvisor name 라벨 미지원으로 0.0 고정.
                       리눅스 환경에서는 container_cpu_usage_seconds_total 쿼리로 교체 가능.
    connection_count: 현재 처리 중인 요청 수 (병목 판단의 핵심 지표).
    """

    # 현재 처리 중인 요청 수 — Semaphore 한도(5)에 얼마나 근접했는지 보여주는 핵심 지표
    # "or vector(0)": 데이터 없을 때 0 보장
    connection_query = "sum(http_requests_in_progress) or vector(0)"

    connection_count = await _query_prometheus(connection_query)

    return SystemMetrics(
        cpu_pct=0.0,      # TODO: 리눅스 환경에서 cAdvisor 연동 시 교체
        mem_pct=0.0,      # TODO: 리눅스 환경에서 cAdvisor 연동 시 교체
        connection_count=int(connection_count),
    )