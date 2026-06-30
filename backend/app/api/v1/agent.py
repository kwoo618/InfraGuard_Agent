

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
import asyncio
import json
import uuid

router = APIRouter(prefix="/agent", tags=["Agent"])


class ApproveRequest(BaseModel):
    task_id: str = Field(..., description="작업 고유 UUID")
    approved: bool = Field(..., description="스케일아웃 승인 여부")


@router.get("/start")
async def start_agent():
    task_id = str(uuid.uuid4())

    async def event_generator():
        try:
            
            yield f"data: {json.dumps({'status': 'running', 'message': 'Locust 부하 테스트를 트리거합니다... (Target: 300 TPS)'})}\n\n"
            await asyncio.sleep(1.5)
            
            
            yield f"data: {json.dumps({'status': 'analyzing', 'message': '[Thought]Prometheus 메트릭 분석 중... CPU 사용량 90% 임계치 초과.'})}\n\n"
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