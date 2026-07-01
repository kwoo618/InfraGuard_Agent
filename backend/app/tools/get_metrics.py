"""
get_metrics — Prometheus에 현재 시스템 상태를 물어보고 SystemMetrics로 포장하는 Tool.

담당: 최강우
소비자: 박정기 (agent/nodes.py) — 이 함수가 반환한 SystemMetrics를 LLM이 읽고 진단함.

동작 순서:
1. Prometheus HTTP API(/api/v1/query)에 PromQL 쿼리를 날린다.
2. cpu_pct, mem_pct, connection_count를 각각 따로 쿼리한다.
3. 응답 JSON에서 숫자만 뽑아 SystemMetrics(schemas.py)로 포장해 반환한다.

주의:
- 컨테이너 이름은 target-server 컨테이너 1개를 기준으로 한다.
  스케일링으로 컨테이너가 여러 개가 되면 평균값으로 집계한다.
- Prometheus가 아직 데이터를 못 모았거나(컨테이너 막 시작) 쿼리 결과가 비어있으면
  0.0 / 0으로 안전하게 기본값 처리한다 (예외로 죽이지 않는다).
"""

import os

import httpx

from app.schemas import SystemMetrics

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
QUERY_TIMEOUT_SECONDS = 5.0

# cAdvisor가 컨테이너에 붙이는 이름 필터. docker-compose.yml의 container_name과 맞춘다.
TARGET_CONTAINER_FILTER = 'name=~"infraguard-target-server.*"'


async def _query_prometheus(promql: str) -> float:
    """
    Prometheus /api/v1/query에 단일 쿼리를 날리고 첫 번째 결과값을 float로 반환한다.
    결과가 없거나 요청이 실패하면 0.0을 반환한다 (에이전트 루프를 막지 않기 위함).
    """
    url = f"{PROMETHEUS_URL}/api/v1/query"

    try:
        async with httpx.AsyncClient(timeout=QUERY_TIMEOUT_SECONDS) as client:
            response = await client.get(url, params={"query": promql})
            response.raise_for_status()
    except httpx.HTTPError:
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
    target-server 컨테이너의 CPU 사용률, 메모리 사용률, 활성 connection 수를
    Prometheus(+cAdvisor)에서 조회해 SystemMetrics로 반환한다.
    """

    # CPU 사용률(%) — cAdvisor가 주는 누적 CPU 시간을 1분 단위 증가율로 변환 후 백분율화
    cpu_query = (
        f'avg(rate(container_cpu_usage_seconds_total{{{TARGET_CONTAINER_FILTER}}}[1m])) * 100'
    )

    # 메모리 사용률(%) — 사용 중인 메모리 / 메모리 상한 (limit이 없으면 0으로 나와 0% 처리됨)
    mem_query = (
        f'avg(container_memory_usage_bytes{{{TARGET_CONTAINER_FILTER}}} '
        f'/ container_spec_memory_limit_bytes{{{TARGET_CONTAINER_FILTER}}}) * 100'
    )

    # 활성 connection 수 — target-server의 /metrics가 노출하는 처리 중인 요청 수
    connection_query = "sum(http_requests_in_progress)"

    cpu_pct = await _query_prometheus(cpu_query)
    mem_pct = await _query_prometheus(mem_query)
    connection_count = await _query_prometheus(connection_query)

    return SystemMetrics(
        cpu_pct=round(cpu_pct, 2),
        mem_pct=round(mem_pct, 2),
        connection_count=int(connection_count),
    )