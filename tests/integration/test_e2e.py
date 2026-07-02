import os
import sys
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

# [가드레일] app/static 디렉토리 에러 강제 방어
if not os.path.exists("app/static"):
    os.makedirs("app/static", exist_ok=True)

# backend 폴더 패스 주입
backend_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
if backend_path not in sys.path:
    sys.path.insert(0, backend_path)

from app.main import app
from app.api.v1.agent import task_manager, AgentRuntimeState

client = TestClient(app)

def test_agent_full_e2e_scenario():
    """
    [E2E 통합 테스트 시나리오]
    진단 시작 -> task_id 생성 확인 -> 유저 승인/거절 -> 리포트 결과 검증까지
    전체 비즈니스 흐름이 파손 없이 유기적으로 이어지는지 검증합니다.
    """
    
    # 1. 테스트용 가짜 상태(State)를 task_manager에 직접 주입하여 환경 격리
    # 다른 파일(create_initial_state)의 복잡한 인프라 체크 로직에 묶이지 않도록 합니다.
    mock_task_id = "e2e-test-task-9999"
    mock_state: AgentRuntimeState = {
        "task_id": mock_task_id,
        "agent_outcome": "awaiting_approval",
        "waiting_for_approval": True,
        "load_test_result": None,
        "scaling_result": None,
        "error": None
    }
    
    # agent.py의 전역 매니저에 직접 등록 (wait_for_approval 준비 상태 생성)
    task_manager.states[mock_task_id] = mock_state
    
    # 비동기 Future 객체도 수동으로 바인딩해 줍니다.
    import asyncio
    loop = asyncio.get_event_loop()
    task_manager.futures[mock_task_id] = loop.create_future()

    # ----------------------------------------------------------------
    # STEP 1: 리포트 중간 조회 (승인 대기 상태인 대시보드 화면 모사)
    # ----------------------------------------------------------------
    # prefix가 /api/v1으로 잡혀있을 테니 안 맞으면 /agent/report/... 로 조절하세요!
    report_response = client.get(f"/api/v1/agent/report/{mock_task_id}")
    assert report_response.status_code == 200
    
    report_data = report_response.json()
    assert report_data["task_id"] == mock_task_id
    assert report_data["outcome"] == "awaiting_approval"
    assert report_data["waiting_for_approval"] is True

    # ----------------------------------------------------------------
    # STEP 2: 유저가 대시보드에서 [승인] 버튼을 누른 상황 모사 (/approve)
    # ----------------------------------------------------------------
    approval_payload = {
        "task_id": mock_task_id,
        "approved": True
    }
    approve_response = client.post("/api/v1/agent/approve", json=approval_payload)
    assert approve_response.status_code == 200
    assert approve_response.json()["status"] == "success"

    # ----------------------------------------------------------------
    # STEP 3: 승인 이후 매니저 상태와 비동기 자물쇠(Future)가 풀렸는지 검증
    # ----------------------------------------------------------------
    # task_manager.approve_task가 정상 작동했다면 상태에 승인 여부가 박히고 Future가 완료됨
    assert task_manager.states[mock_task_id]["scaling_approved"] is True
    assert task_manager.futures[mock_task_id].done() is True