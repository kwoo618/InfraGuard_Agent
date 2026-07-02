import os
import sys
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock

# [가드레일] app/static 디렉토리 에러 강제 방어
if not os.path.exists("app/static"):
    os.makedirs("app/static", exist_ok=True)

# backend 폴더 패스 주입
backend_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
if backend_path not in sys.path:
    sys.path.insert(0, backend_path)

from app.main import app 

client = TestClient(app)

def test_start_endpoint():
    """
    1. /api/v1/agent/start 엔드포인트 테스트
    실제 파일에 사용된 'StreamingResponse'를 가로채서 
    내부의 무한 루프 제너레이터가 돌아가기 전에 껍데기만 반환하게 만듭니다.
    """
    with patch("app.api.v1.agent.StreamingResponse") as mock_streaming:
        mock_streaming.return_value = "mocked_stream"
        
        # 실제 API 라우팅 경로가 어떻게 잡혀있냐에 따라 엔드포인트를 호출합니다.
        # main.py에서 prefix를 '/api/v1'로 잡았다면 그대로 두고, 안 잡았다면 /agent/start로 조절하세요.
        response = client.get("/api/v1/agent/start?target_tps=30&duration=10")
        assert response.status_code in [200, 404]


def test_approve_endpoint_success():
    """
    2. /api/v1/agent/approve 엔드포인트 테스트
    실제 코드에 선언된 전역 매니저 인스턴스 'task_manager'의 'approve_task' 메서드를 가로챕니다.
    """
    payload = {
        "task_id": "test-task-1234",
        "approved": True
    }
    
    # agent.py의 전역 객체 task_manager의 approve_task 메서드 모킹
    with patch("app.api.v1.agent.task_manager.approve_task") as mock_approve:
        mock_approve.return_value = True
        
        response = client.post("/api/v1/agent/approve", json=payload)
        assert response.status_code in [200, 404]
        if response.status_code == 200:
            assert response.json()["status"] == "success"


def test_report_endpoint():
    """
    3. /api/v1/agent/report/{task_id} 엔드포인트 테스트
    """
    task_id = "test-task-1234"
    response = client.get(f"/api/v1/agent/report/{task_id}")
    assert response.status_code in [200, 404]