# 최소명 - /start /approve /report

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
import asyncio
import json
import uuid

router = APIRouter(prefix="/agent", tags=["Agent"])

# API 통신에 사용할 Pydantic 스키마 (HITL 승인용)
class ApproveRequest(BaseModel):
    task_id: str = Field(..., description="작업 고유 UUID")
    approved: bool = Field(..., description="스케일아웃 승인 여부")

# ④-1. 진단 및 부하 테스트 시작 (SSE 스트리밍)
@router.get("/start")
async def start_agent():
    task_id = str(uuid.uuid4())

    async def event_generator():
        try:
            # [이하은님 영역 연동부 미리 구성]
            yield f"data: {json.dumps({'status': 'running', 'message': 'Locust 부하 테스트를 트리거합니다... (Target: 300 TPS)'})}\n\n"
            await asyncio.sleep(1.5)
            
            # [박정기님 / 최강우님 영역 연동부 미리 구성]
            yield f"data: {json.dumps({'status': 'analyzing', 'message': '[Thought]Prometheus 메트릭 분석 중... CPU 사용량 90% 임계치 초과.'})}\n\n"
            await asyncio.sleep(1.5)
            
            # [HITL 가드레일 신호 발동] -> 프론트 UI에서 팝업을 띄우게 만듦
            yield f"data: {json.dumps({'status': 'need_approval', 'task_id': task_id, 'message': '컨테이너를 3대로 증설(Scale-out)할까요?'})}\n\n"
        except asyncio.CancelledError:
            print("Client disconnected from SSE stream.")

    return StreamingResponse(event_generator(), media_type="text/event-stream")

# ④-2. HITL 승인 버튼 처리
@router.post("/approve")
async def approve_scaling(payload: ApproveRequest):
    if payload.approved:
        # [최강우님 영역 연동부] -> 여기서 scale_service.py의 함수를 호출하게 됨
        return {
            "status": "success",
            "task_id": payload.task_id,
            "message": "scale_service 로직 호출 성공! 인프라가 증설되었습니다."
        }
    return {"status": "rejected", "message": "유저가 증설 요청을 거부했습니다."}

# ④-3. 최종 리포트 조회
@router.get("/report/{task_id}")
async def get_report(task_id: str):
    return {
        "task_id": task_id,
        "status": "completed",
        "summary": "generate_plan 및 에이전트 최종 진단 결과 리포트 샘플입니다."
    }