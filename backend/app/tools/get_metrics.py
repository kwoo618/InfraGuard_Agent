"""
get_metrics — Prometheus에 현재 시스템 상태를 물어보고 SystemMetrics로 포장하는 Tool.

담당: 최강우
소비자: 박정기 (agent/nodes.py) — 이 함수가 반환한 SystemMetrics를 LLM이 읽고 진단함.

동작 순서:
1. Prometheus HTTP API(/api/v1/query)에 PromQL 쿼리를 날린다.
2. 활성 connection 수(전체 replica의 처리 중 + 대기 중인 요청 수)를 쿼리한다.
3. 응답 JSON에서 숫자만 뽑아 SystemMetrics(schemas.py)로 포장해 반환한다.

주의:
- 윈도우 Docker Desktop 환경에서 cAdvisor가 name 라벨을 붙이지 않아
  컨테이너별 CPU/메모리 필터링이 불가능하다.
- 대신 target-server가 /metrics로 직접 노출하는 애플리케이션 레벨 메트릭을 사용한다.
  (prometheus-fastapi-instrumentator 제공)
- cpu_pct, mem_pct는 0.0으로 고정 반환한다. 측정값이 아니므로 RESOURCE_METRICS_COLLECTED=False로
  표시하고, agent/prompts.py는 이 값을 "측정 불가"로 바꿔 LLM에 전달한다 (docs/02 ISSUE-10).
  향후 리눅스 환경에서 cAdvisor 연동 시 교체하고, 같은 변경에서 True로 바꾼다.
- Prometheus가 아직 데이터를 못 모았거나 쿼리 결과가 비어있으면
  0.0 / 0으로 안전하게 기본값 처리한다 (예외로 죽이지 않는다).
"""

import os

import httpx

from app.schemas import SystemMetrics

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
QUERY_TIMEOUT_SECONDS = 5.0

# cpu_pct / mem_pct를 실제로 수집하는지 여부.
# 지금은 0.0 고정값(측정값 아님)이라 False. 프롬프트가 이 값을 보고 "측정 불가"로 표시한다.
# cAdvisor 등으로 실제 값을 채우는 변경에서 이 값을 True로 바꾼다. (docs/02 ISSUE-10)
RESOURCE_METRICS_COLLECTED = False


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
                       측정값이 아니다 (RESOURCE_METRICS_COLLECTED=False).
                       리눅스 환경에서는 container_cpu_usage_seconds_total 쿼리로 교체 가능.
    connection_count: target-server 전체 replica에서 처리 중 + 대기 중인 요청 수의 합 (순간값).
    """

    # 전체 replica의 in-progress 게이지 합 — 앱에 들어와 처리 중이거나 처리를 기다리는 요청 수
    # "or vector(0)": 데이터 없을 때 0 보장
    connection_query = "sum(http_requests_in_progress) or vector(0)"

    connection_count = await _query_prometheus(connection_query)

    return SystemMetrics(
        cpu_pct=0.0,      # 측정값 아님 (RESOURCE_METRICS_COLLECTED=False). cAdvisor 연동 시 교체
        mem_pct=0.0,      # 측정값 아님 (RESOURCE_METRICS_COLLECTED=False). cAdvisor 연동 시 교체
        connection_count=int(connection_count),
    )


# ---------------------------------------------------------------------
# 서버별 요청 분산 (결과 패널 그래프, docs/03 Phase 4)
# ---------------------------------------------------------------------
# 서버별로 세는 핸들러. Locust 시나리오 중 /health는 뺀다:
# docker-compose 헬스체크가 replica마다 5초에 한 번 /health를 호출해서 부하 요청과 구분할 수 없다.
# /metrics(Prometheus 수집 요청)도 뺀다. 그래프에 이 제외 사실을 함께 표시한다.
LOAD_HANDLERS = ("/light", "/heavy", "/flaky")
EXCLUDED_HANDLERS = ("/health", "/metrics")


async def get_request_counts_by_instance() -> dict[str, float] | None:
    """
    target-server 인스턴스(replica)별 누적 요청 수 (LOAD_HANDLERS만, 카운터 원본값).

    부하 직전과 부하가 끝난 뒤(다음 scrape 이후) 두 번 조회해 차이를 보면
    부하 요청이 서버별로 어떻게 나뉘었는지 알 수 있다 (instance_request_deltas).
    조회 실패는 None이다. 측정값이 아니므로 0으로 채우지 않는다.
    아직 요청을 한 번도 받지 않은 인스턴스는 결과에 없다.
    """

    handlers = "|".join(LOAD_HANDLERS)
    promql = (
        f'sum by (instance) (http_requests_total{{job="target-server", handler=~"{handlers}"}})'
    )

    try:
        async with httpx.AsyncClient(timeout=QUERY_TIMEOUT_SECONDS) as client:
            response = await client.get(
                f"{PROMETHEUS_URL}/api/v1/query",
                params={"query": promql},
            )

        if response.status_code != 200:
            return None

        result = response.json().get("data", {}).get("result", [])

        return {
            item["metric"]["instance"]: float(item["value"][1])
            for item in result
        }
    except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError):
        return None


def instance_request_deltas(
    before: dict[str, float] | None,
    after: dict[str, float] | None,
) -> dict[str, int | None] | None:
    """
    부하 전후 카운터 차이 = 인스턴스별 부하 요청 수.

    - before에 없는 인스턴스(스케일로 새로 뜬 replica)는 0에서 시작한 것으로 본다.
      LOAD_HANDLERS 요청은 부하 중에만 들어오기 때문이다.
    - 차이가 음수(컨테이너 재시작으로 카운터 초기화)면 그 인스턴스는 None이다.
    - 어느 한쪽 조회가 실패했으면 None.
    """

    if before is None or after is None:
        return None

    deltas: dict[str, int | None] = {}

    for instance, value in sorted(after.items()):
        delta = value - before.get(instance, 0.0)
        deltas[instance] = round(delta) if delta >= 0 else None

    return deltas
