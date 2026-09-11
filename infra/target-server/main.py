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

# /metrics 엔드포인트 자동 노출 (요청 수, latency 등) — Prometheus가 scrape
# in-progress 게이지는 기본값이 꺼져 있고 기본 이름도 http_requests_inprogress라서,
# get_metrics.py·Grafana 쿼리가 쓰는 http_requests_in_progress 이름으로 명시해 켠다. (ISSUE-6)
Instrumentator(
    should_instrument_requests_inprogress=True,
    inprogress_name="http_requests_in_progress",
).instrument(app).expose(app)

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
