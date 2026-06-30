import asyncio
import json
import uuid
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
import httpx

router = APIRouter(prefix="/agent", tags=["Agent"])

PROMETHEUS_HEALTH_URL = "http://localhost:9090/-/healthy"


class ApproveRequest(BaseModel):
    task_id: str = Field(..., description="작업 고유 UUID")
    approved: bool = Field(..., description="스케일아웃 승인 여부")


@router.get("/start")
async def start_agent():
    task_id = str(uuid.uuid4())

    async def event_generator():
        try:
            yield f"data: {json.dumps({'status': 'running', 'message': '에이전트를 시작합니다. 로컬 인프라 상태를 점검합니다...'})}\n\n"
            await asyncio.sleep(1.5)
            
            async with httpx.AsyncClient() as client:
                try:
                    response = await client.get(PROMETHEUS_HEALTH_URL, timeout=2.0)
                    
                    if response.status_code == 200:
                        yield f"data: {json.dumps({'status': 'analyzing', 'message': '[성공] 도커 Prometheus 서버 연동 완료. 실시간 메트릭 수집을 시작합니다.'})}\n\n"
                    else:
                        yield f"data: {json.dumps({'status': 'error', 'message': f'[경고] Prometheus 서버 응답 이상 (Status: {response.status_code})'})}\n\n"
                
                except (httpx.ConnectError, httpx.TimeoutException):
                    yield f"data: {json.dumps({'status': 'error', 'message': '[에러] 도커 인프라가 꺼져 있습니다! docker compose up -d를 확인하세요.'})}\n\n"
                    return
            
            await asyncio.sleep(1.5)
            
            yield f"data: {json.dumps({'status': 'analyzing', 'message': '[Thought] 타겟 서버 부하 감지 시뮬레이션 중... CPU 사용량 임계치(90%) 초과.'})}\n\n"
            await asyncio.sleep(1.5)
            
            yield f"data: {json.dumps({'status': 'need_approval', 'task_id': task_id, 'message': '컨테이너를 3대로 증설(Scale-out)할까요?'})}\n\n"
            
        except asyncio.CancelledError:
            print("Client disconnected from SSE stream.")

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.post("/approve")
async def approve_scaling(payload: ApproveRequest):
    if payload.approved:
        return {
            "status": "success",
            "task_id": payload.task_id,
            "message": "scale_service 로직 호출 성공! 인프라가 증설되었습니다."
        }
    return {"status": "rejected", "message": "유저가 증설 요청을 거부했습니다."}


@router.get("/report/{task_id}")
async def get_report(task_id: str):
    return {
        "task_id": task_id,
        "status": "completed",
        "summary": "generate_plan 및 에이전트 최종 진단 결과 리포트 샘플입니다."
    }