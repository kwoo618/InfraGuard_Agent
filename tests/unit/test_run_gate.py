"""
동시 실행 방지 테스트 (docs/02 ISSUE-17).

- 에이전트 실행 중(승인 대기 포함)이면 /agent/start 409
- 연결이 끊긴 실행의 부하 테스트(Locust 스레드)가 끝날 때까지 새 실행·서버 초기화 409, 끝나면 다시 허용
- start_agent 확인 뒤 스트림 시작 전에 끼어든 요청은 스트림 첫 이벤트에서 거부 (결과 파일 없음)

실제 Locust·Docker·LLM은 실행하지 않는다. Locust 실행은 스레드 이벤트로 멈춰 두는 가짜 함수로 흉내 낸다.
아래 수치는 테스트 입력값이며 측정값이 아니다.
"""

import asyncio
import json
import threading

import httpx
import pytest
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from app.api.v1 import agent, run_history
from app.api.v1.agent import load_tests, run_records, task_manager
from app.api.v1.run_history import RunRecorder
from app.main import app
from app.schemas import LoadTestResult, ScalingResult

# agent.httpx.AsyncClient를 바꾸면 httpx 모듈 전체가 바뀌므로 원본을 먼저 잡아 둔다.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _clear() -> None:
    run_records.clear()
    load_tests.clear()
    task_manager.states.clear()
    task_manager.futures.clear()


@pytest.fixture(autouse=True)
def results_dir(tmp_path, monkeypatch):
    directory = tmp_path / "results"

    monkeypatch.setattr(run_history, "RESULTS_DIR", directory)
    monkeypatch.setattr(agent, "_replica_reset_in_progress", False)
    monkeypatch.setattr(agent, "DEBUG_ENDPOINTS_ENABLED", False)
    monkeypatch.setattr(agent, "get_current_replicas", lambda: 1)

    _clear()
    yield directory
    _clear()


def _recorder(task_id: str = "running-task") -> RunRecorder:
    recorder = RunRecorder(
        task_id=task_id,
        target_tps=50,
        duration=10,
        force_scaling_requested=False,
        debug_endpoints_enabled=False,
    )
    run_records[task_id] = recorder
    return recorder


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("조건을 기다리다 시간이 초과됐다")
        await asyncio.sleep(0.01)


def test_start_rejected_while_agent_running():
    _recorder()

    response = TestClient(app).get("/api/v1/agent/start", params={"target_tps": 5, "duration": 1})

    assert response.status_code == 409
    assert "다른 에이전트 실행" in response.json()["detail"]


def test_start_and_reset_rejected_while_load_test_thread_running(monkeypatch):
    calls = []

    async def fake_scale_service(target_replicas: int) -> ScalingResult:
        calls.append(target_replicas)
        return ScalingResult(before_replicas=2, after_replicas=target_replicas, success=True)

    monkeypatch.setattr(agent, "scale_service", fake_scale_service)
    client = TestClient(app)

    # 끊긴 실행의 Locust 스레드가 남아 있는 상태
    load_tests.enter("orphan-task")

    response = client.get("/api/v1/agent/start", params={"target_tps": 5, "duration": 1})

    assert response.status_code == 409
    assert "부하 테스트" in response.json()["detail"]
    assert client.get("/api/v1/agent/replicas").json() == {
        "replicas": 1,
        "busy": True,
        "busy_reason": "load_test_running",
    }
    assert client.post("/api/v1/agent/replicas/reset").status_code == 409
    assert calls == []

    # Locust 스레드가 끝나면 다시 허용된다
    load_tests.exit("orphan-task")

    assert client.get("/api/v1/agent/replicas").json() == {
        "replicas": 1,
        "busy": False,
        "busy_reason": None,
    }


def test_load_test_tracker_counts_per_task():
    load_tests.enter("a")
    load_tests.enter("a")
    load_tests.enter("b")
    load_tests.exit("a")

    assert sorted(load_tests.active()) == ["a", "b"]

    load_tests.exit("a")
    load_tests.exit("b")

    assert load_tests.active() == []


async def test_stream_rejected_when_another_run_is_active(results_dir):
    """start_agent 확인 뒤에 다른 실행이 먼저 시작된 경우: 스트림 첫 이벤트에서 거부하고 실행을 만들지 않는다."""

    _recorder()

    frames = [
        json.loads(frame.removeprefix("data: ").strip())
        async for frame in agent.start_agent_stream(target_tps=5, duration=1)
    ]

    assert len(frames) == 1
    assert frames[0]["status"] == "failed"
    assert frames[0]["task_id"] is None
    assert "다른 에이전트 실행" in frames[0]["message"]
    assert list(run_records) == ["running-task"]
    assert list(results_dir.glob("*.json")) == []


async def test_disconnected_run_blocks_new_runs_until_locust_finishes(results_dir, monkeypatch):
    """
    부하 테스트 중 연결이 끊긴 실행: 결과 파일은 stream_closed로 남고, 그 실행의 Locust 스레드가
    끝날 때까지 새 실행과 서버 초기화를 409로 거부한다. 스레드가 끝나면 다시 시작할 수 있다.
    """

    release = threading.Event()

    def blocking_detailed(target_tps, duration):
        # Locust 실행을 흉내 낸다: 풀어줄 때까지 이 스레드가 끝나지 않는다
        release.wait(timeout=10)
        result = LoadTestResult(
            tps=50.0,
            latency_p95=900.0,
            latency_avg=300.0,
            error_rate=0.0,
            duration=duration,
            total_requests=500,
        )
        return result, {"timeseries": None, "endpoints": None}

    async def empty_counts():
        return {}

    async def fake_run_diagnosis(state, load_test_node=None, **kwargs):
        state.update(await load_test_node(state))
        state.update(agent_outcome="diagnosed", final_answer="진단 완료")
        return state

    def healthy_client(*args, **kwargs):
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(lambda request: httpx.Response(200)))

    monkeypatch.setattr(agent.httpx, "AsyncClient", healthy_client)
    monkeypatch.setattr(agent, "run_load_test_detailed", blocking_detailed)
    monkeypatch.setattr(agent, "get_request_counts_by_instance", empty_counts)
    monkeypatch.setattr(agent, "run_diagnosis", fake_run_diagnosis)

    stream = agent.start_agent_stream(target_tps=5, duration=1)

    try:
        async for frame in stream:
            if "부하 테스트 진행 중" in frame:
                break

        # 부하 테스트 단계를 진행시키고 Locust 스레드가 시작될 때까지 기다린다
        pending = asyncio.ensure_future(stream.__anext__())
        await _wait_until(lambda: bool(load_tests.active()))

        # 클라이언트 연결이 끊긴 경우: 스트림을 처리하던 작업이 취소된다
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending

        [path] = list(results_dir.glob("*.json"))
        data = json.loads(path.read_text(encoding="utf-8"))

        assert data["outcome"]["end_reason"] == "stream_closed"
        assert data["measurement_history"] == []

        # 실행 기록은 끝났지만 Locust 스레드는 아직 돈다 → 새 실행·초기화 거부
        assert agent._busy_reason() == "load_test_running"

        with pytest.raises(HTTPException) as start_exc:
            await agent.start_agent(target_tps=5, duration=1)

        assert start_exc.value.status_code == 409
        assert "부하 테스트" in start_exc.value.detail

        with pytest.raises(HTTPException) as reset_exc:
            await agent.reset_replicas()

        assert reset_exc.value.status_code == 409

        # Locust가 끝나면 다시 시작할 수 있다 (스트림은 만들기만 하고 실행하지 않는다)
        release.set()
        await _wait_until(lambda: not load_tests.active())

        assert agent._busy_reason() is None
        assert isinstance(await agent.start_agent(target_tps=5, duration=1), StreamingResponse)
    finally:
        release.set()
