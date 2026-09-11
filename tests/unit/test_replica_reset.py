"""
서버 수 조회(GET /agent/replicas)와 1대 초기화(POST /agent/replicas/reset) 테스트.

초기화는 측정 회차 사이 사람의 수동 조작이다. 에이전트 실행 중(승인 대기 포함)이거나
다른 초기화가 진행 중이면 409로 거부하고, 결과 파일(results/)에는 기록하지 않는다.
끊긴 실행의 부하 테스트가 남아 있을 때의 거부는 test_run_gate.py에서 확인한다.

실제 Docker는 실행하지 않는다. scale_service와 replica 조회를 가짜로 바꾸고 결과 파일은 tmp_path에만 쓴다.
"""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.agent.state import create_initial_state
from app.api.v1 import agent, run_history
from app.api.v1.agent import load_tests, run_records
from app.api.v1.run_history import RunRecorder
from app.main import app
from app.schemas import ScalingResult


@pytest.fixture(autouse=True)
def results_dir(tmp_path, monkeypatch):
    directory = tmp_path / "results"

    monkeypatch.setattr(run_history, "RESULTS_DIR", directory)
    monkeypatch.setattr(agent, "_replica_reset_in_progress", False)

    run_records.clear()
    load_tests.clear()
    yield directory
    run_records.clear()
    load_tests.clear()


@pytest.fixture
def scale_calls(monkeypatch):
    """scale_service를 가짜로 바꾸고 호출 인자를 모은다."""

    calls = []

    async def fake_scale_service(target_replicas: int) -> ScalingResult:
        calls.append(target_replicas)
        return ScalingResult(
            before_replicas=3,
            after_replicas=target_replicas,
            success=True,
        )

    monkeypatch.setattr(agent, "scale_service", fake_scale_service)
    return calls


def _recorder(task_id: str) -> RunRecorder:
    recorder = RunRecorder(
        task_id=task_id,
        target_tps=50,
        duration=10,
        force_scaling_requested=False,
        debug_endpoints_enabled=False,
    )
    run_records[task_id] = recorder
    return recorder


def _state():
    return create_initial_state(target_tps=50, duration=10)


async def test_reset_rejected_while_agent_running(scale_calls):
    _recorder("running-task")

    with pytest.raises(HTTPException) as exc_info:
        await agent.reset_replicas()

    assert exc_info.value.status_code == 409
    assert scale_calls == []


def test_reset_rejected_while_waiting_for_approval(scale_calls):
    recorder = _recorder("waiting-task")
    recorder.open_approval(_state())

    response = TestClient(app).post("/api/v1/agent/replicas/reset")

    assert response.status_code == 409
    assert scale_calls == []


async def test_reset_rejected_while_another_reset_in_progress(scale_calls, monkeypatch):
    monkeypatch.setattr(agent, "_replica_reset_in_progress", True)

    with pytest.raises(HTTPException) as exc_info:
        await agent.reset_replicas()

    assert exc_info.value.status_code == 409
    assert scale_calls == []


def test_reset_scales_to_one_when_idle(scale_calls, results_dir):
    # 끝난 실행의 기록기는 초기화를 막지 않는다
    finished = _recorder("finished-task")
    finished.finalize(_state(), "no_scaling_proposed")
    files_before = sorted(results_dir.glob("*.json"))

    response = TestClient(app).post("/api/v1/agent/replicas/reset")

    assert response.status_code == 200
    assert response.json() == {
        "before_replicas": 3,
        "after_replicas": 1,
        "success": True,
        "error_message": None,
    }
    assert scale_calls == [1]
    # 사람의 수동 조작이라 결과 파일을 새로 만들지 않는다
    assert sorted(results_dir.glob("*.json")) == files_before
    # 끝나면 진행 중 표시가 풀린다
    assert agent._replica_reset_in_progress is False


@pytest.mark.parametrize(("docker_value", "expected"), [(2, 2), (0, None)])
def test_get_replicas(monkeypatch, docker_value, expected):
    # scale_service.get_current_replicas는 조회 실패 시 0을 반환한다 → null
    monkeypatch.setattr(agent, "get_current_replicas", lambda: docker_value)

    response = TestClient(app).get("/api/v1/agent/replicas")

    assert response.status_code == 200
    assert response.json() == {"replicas": expected, "busy": False, "busy_reason": None}


def test_get_replicas_reports_busy_while_running(monkeypatch):
    monkeypatch.setattr(agent, "get_current_replicas", lambda: 1)
    _recorder("running-task")

    response = TestClient(app).get("/api/v1/agent/replicas")

    assert response.json() == {"replicas": 1, "busy": True, "busy_reason": "agent_running"}
