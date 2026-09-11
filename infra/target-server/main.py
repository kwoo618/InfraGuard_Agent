"""
target-server — 부하 테스트 대상이 되는 샘플 FastAPI 앱.

InfraGuard Agent가 진단할 '대상 시스템' 역할을 한다.
의도적으로 DB Connection Pool 고갈을 흉내내는 /heavy 엔드포인트를 포함해
Locust로 부하를 줄 때 실제로 병목이 발생하도록 만든다.
"""

import asyncio
import random

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator

app = FastAPI(title="InfraGuard Target Server")

# http_request_duration_seconds 히스토그램 버킷(초). 라이브러리 기본값 (0.1, 0.5, 1)은
# 1초가 최상위라 Grafana P95가 1000ms에서 잘렸다. (docs/02 ISSUE-8)
# - /light(sleep 10ms)·/flaky(sleep 50ms) 구간: 5~10ms 간격
# - /heavy 설계 처리시간 0.2~0.6초와 P95 SLO 기본값 1초 경계: 0.1초 간격, 1.0을 경계로 둔다
# - 실측 1대 50명 /heavy P95 약 3.3초 구간(docs/evidence): 0.25~0.5초 간격
# - 30초를 넘는 값은 histogram_quantile이 30초로 표시한다 (최상위 버킷 한계)
# Grafana P95와 Locust P95를 비교하기 전에 정한 값이다. 비교 결과를 보고 조정하지 않는다.
LATENCY_BUCKETS = (
    0.005, 0.01, 0.015, 0.02, 0.03, 0.05, 0.06, 0.075,
    0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.6, 0.75,
    1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0,
    6.0, 7.5, 10.0, 15.0, 30.0,
)

# /metrics 엔드포인트 자동 노출 (요청 수, latency 등) — Prometheus가 scrape
# in-progress 게이지는 기본값이 꺼져 있고 기본 이름도 http_requests_inprogress라서,
# get_metrics.py·Grafana 쿼리가 쓰는 http_requests_in_progress 이름으로 명시해 켠다. (ISSUE-6)
Instrumentator(
    should_instrument_requests_inprogress=True,
    inprogress_name="http_requests_in_progress",
).instrument(app, latency_lowr_buckets=LATENCY_BUCKETS).expose(app)

# 의도적으로 작은 커넥션 풀 흉내 (semaphore로 동시 처리량 제한)
CONNECTION_POOL_SIZE = 5
_pool = asyncio.Semaphore(CONNECTION_POOL_SIZE)


@app.get("/health")
async def health():
    """가벼운 헬스체크 — 항상 빠르게 응답"""
    return {"status": "ok"}


@app.get("/light")
async def light():
    """가벼운 엔드포인트 — 기본 트래픽용"""
    await asyncio.sleep(0.01)
    return {"status": "ok", "type": "light"}


@app.get("/heavy")
async def heavy():
    """
    무거운 엔드포인트 — DB connection pool 고갈을 흉내낸다.
    동시 요청이 CONNECTION_POOL_SIZE를 넘으면 대기열이 쌓여 latency가 급증한다.
    InfraGuard Agent가 진단해야 할 '병목 시나리오'.
    """
    async with _pool:
        # 임의의 처리 시간 (DB 쿼리 흉내)
        processing_time = random.uniform(0.2, 0.6)
        await asyncio.sleep(processing_time)
        return {"status": "ok", "type": "heavy", "processed_in": processing_time}


@app.get("/flaky")
async def flaky():
    """
    가끔 에러를 내는 엔드포인트 — error_rate 메트릭 테스트용.

    주의: FastAPI에서 `return {...}, 500` 튜플은 상태코드로 해석되지 않고
    본문으로 직렬화되어 200이 나간다. 실제 500을 내려면 JSONResponse로
    status_code를 명시해야 error_rate/Locust Failure Count가 잡힌다.
    """
    if random.random() < 0.05:
        return JSONResponse(status_code=500, content={"status": "error", "code": 500})
    await asyncio.sleep(0.05)
    return {"status": "ok", "type": "flaky"}


@app.get("/")
async def root():
    return {
        "service": "InfraGuard Target Server",
        "endpoints": ["/health", "/light", "/heavy", "/flaky"],
    }
