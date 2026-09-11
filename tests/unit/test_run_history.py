"""
Phase 3(#75) 라운드별 측정 이력 + 결과 파일 저장 테스트.

- RunRecorder 단위 동작: 측정 불가 표기, replica null, 중복 방지, 파일 저장·실패 처리
- start_agent_stream 전체 경로: 미제안, 승인→스케일링, 거절, 실패, forced, 저장 실패, 스트림 종료
- /report 결과 패널용 필드(Phase 4)가 결과 파일과 같은지
- 저신뢰 확인 게이트(#83): confidence < 0.6 승인은 확인 플래그 필요, 0.6은 게이트 없음, 거절은 항상 허용

실제 Locust·Docker·LLM은 실행하지 않는다. engine 함수·replica 조회·Solar 호출을 가짜로 바꾸고,
Prometheus 헬스체크는 httpx.MockTransport로 응답한다. 결과 파일은 tmp_path에만 쓴다.
아래 수치는 테스트 입력값이며 측정값이 아니다.
"""

import asyncio
import json
import re
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.agent import nodes
from app.agent.state import create_initial_state
from app.api.v1 import agent, run_history
from app.api.v1.agent import before_measurements, run_records, task_manager
from app.main import app
from app.api.v1.run_history import (
    UNMEASURED_LABEL,
    RunRecorder,
    read_locust_task_weights,
)
from app.schemas import (
    BottleneckReport,
    LoadTestResult,
    ScalingResult,
    SystemMetrics,
)
from app.tools.run_load_test import LoadTestError

# agent.httpx.AsyncClient를 바꾸면 httpx 모듈 전체가 바뀌므로 원본을 먼저 잡아 둔다.
_REAL_ASYNC_CLIENT = httpx.AsyncClient

# /report의 기존 응답 필드 (삭제·이름 변경 금지)
EXISTING_REPORT_FIELDS = {
    "task_id",
    "outcome",
    "waiting_for_approval",
    "loop_count",
    "measurement",
    "measurement_after",
    "improvement",
    "bottleneck",
    "action",
    "optimization_plan",
    "summary",
    "error",
}

SCALING_PLAN = {
    "service_name": "target-server",
    "current_replicas": 1,
    "desired_replicas": 2,
    "reason": "P95 SLO 위반",
}


def _clear_agent_storage() -> None:
    agent.load_tests.clear()
    task_manager.states.clear()
    task_manager.futures.clear()
    task_manager.low_confidence.clear()
    task_manager.acknowledgements.clear()
    before_measurements.clear()
    run_records.clear()


@pytest.fixture(autouse=True)
def results_dir(tmp_path, monkeypatch):
    """결과 파일을 tmp_path에만 쓰고, 전역 task 저장소가 테스트 간에 섞이지 않게 한다."""

    directory = tmp_path / "results"

    monkeypatch.setattr(run_history, "RESULTS_DIR", directory)
    monkeypatch.setattr(run_history, "RESOURCE_METRICS_COLLECTED", False)
    monkeypatch.setattr(agent, "DEBUG_ENDPOINTS_ENABLED", False)
    monkeypatch.delenv("P95_SLO_MS", raising=False)

    _clear_agent_storage()
    yield directory
    _clear_agent_storage()


@pytest.fixture
def fake_infra(monkeypatch):
    """Prometheus 헬스체크 200, replica 1, 재검증 Solar 응답을 가짜로 둔다."""

    def healthy_client(*args, **kwargs):
        return _REAL_ASYNC_CLIENT(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200)
            )
        )

    async def fake_solar(system_prompt: str, user_prompt: str) -> str:
        return json.dumps(
            {
                "summary": "재검증 요약",
                "performance_improved": True,
                "tps_change": 1.0,
                "additional_action_required": False,
                "recommended_action": None,
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(agent.httpx, "AsyncClient", healthy_client)
    monkeypatch.setattr(agent, "get_current_replicas", lambda: 1)
    monkeypatch.setattr(agent, "call_solar_api", fake_solar)


# ----------------------------------------------------------------------
# 테스트 데이터
# ----------------------------------------------------------------------


def _load_result(tps: float, p95: float) -> LoadTestResult:
    return LoadTestResult(
        tps=tps,
        latency_p95=p95,
        latency_avg=p95 / 2,
        error_rate=0.004,
        duration=10,
        total_requests=int(tps * 10),
    )


def _metrics(connections: int) -> SystemMetrics:
    return SystemMetrics(
        cpu_pct=0.0,
        mem_pct=0.0,
        connection_count=connections,
        timestamp="2026-09-11T03:00:00",
    )


def _report(requires_scaling: bool, confidence: float = 0.8) -> BottleneckReport:
    return BottleneckReport(
        cause="테스트 원인",
        severity="high",
        recommendation="테스트 권장 조치",
        confidence=confidence,
        requires_scaling=requires_scaling,
    )


def _state_with(load, metrics, report=None):
    state = create_initial_state(target_tps=50, duration=10)
    state.update(
        load_test_result=load,
        system_metrics=metrics,
        bottleneck_report=report,
    )
    return state


def _recorder(task_id: str = "task-1") -> RunRecorder:
    return RunRecorder(
        task_id=task_id,
        target_tps=50,
        duration=10,
        force_scaling_requested=True,
        debug_endpoints_enabled=False,
    )


def _fake_diagnosis(
    *,
    requires_scaling: bool = False,
    confidence: float = 0.8,
    fail_before_load: bool = False,
    fail_after_load: bool = False,
):
    # agent.py는 부하 테스트 수집용 load_test_node를 kwargs로 넘긴다. 이 가짜는 부하 테스트를 흉내만 내므로 쓰지 않는다
    async def fake_run_diagnosis(state, **kwargs):
        if fail_before_load:
            state.update(
                agent_outcome="failed",
                error="부하 테스트 실행 실패: 테스트",
            )
            return state

        state["load_test_result"] = _load_result(50.0, 3000.0)
        state["system_metrics"] = _metrics(40)

        if fail_after_load:
            state.update(
                agent_outcome="failed",
                error="LLM 병목 분석 실패: 테스트",
                loop_count=1,
            )
            return state

        state["bottleneck_report"] = _report(requires_scaling, confidence)
        state["loop_count"] += 1

        if requires_scaling:
            state.update(
                agent_outcome="awaiting_approval",
                scaling_required=True,
                waiting_for_approval=True,
                scaling_plan=dict(SCALING_PLAN),
                final_answer="1개에서 2개로 확장을 제안합니다.",
            )
        else:
            state.update(
                agent_outcome="diagnosed",
                final_answer="진단 완료: 병목 없음",
            )

        return state

    return fake_run_diagnosis


def _fake_resume():
    async def fake_resume_after_approval(state, approved, **kwargs):
        state["scaling_approved"] = approved
        state["waiting_for_approval"] = False

        if not approved:
            state.update(
                agent_outcome="diagnosed",
                scaling_required=False,
                final_answer="사용자가 스케일링 제안을 거절했습니다.",
            )
            return state

        state["scaling_result"] = ScalingResult(
            before_replicas=1,
            after_replicas=2,
            success=True,
        )
        state["scaling_count"] += 1
        state["load_test_result"] = _load_result(98.0, 980.0)
        state["system_metrics"] = _metrics(12)

        # agent.py가 넘긴 재검증 호출자를 engine처럼 호출한다
        await kwargs["revalidation_caller"]("system", "user")

        state.update(
            agent_outcome="scaled",
            scaling_required=False,
            final_answer="스케일링 후 재검증 완료: 재검증 요약",
        )
        return state

    return fake_resume_after_approval


def _parse(frame: str) -> dict:
    return json.loads(frame.removeprefix("data: ").strip())


async def _run_stream(*, decision=None, force_scaling=False):
    """SSE를 끝까지 읽고, need_approval을 받으면 decision으로 승인/거절한다."""

    events = []

    async for frame in agent.start_agent_stream(
        target_tps=50,
        duration=10,
        force_scaling=force_scaling,
    ):
        event = _parse(frame)
        events.append(event)

        if event["status"] == "need_approval":
            assert decision is not None, "예상하지 않은 승인 요청"
            assert (
                task_manager.approve_task(event["task_id"], decision)
                == "success"
            )

    return events


def _only_result_file(directory: Path) -> dict:
    files = sorted(directory.glob("*.json"))
    assert len(files) == 1, files
    return json.loads(files[0].read_text(encoding="utf-8"))


async def _assert_report_matches_file(data: dict) -> dict:
    """/report 기존 필드가 유지되고, 결과 패널용 필드(Phase 4)가 결과 파일과 같은지 확인한다."""

    report = await agent.get_report(data["task_id"])

    assert EXISTING_REPORT_FIELDS <= report.keys()
    assert report["measurement_history"] == data["measurement_history"]
    assert report["forced_scaling"] == data["forced_scaling"]
    assert report["conditions"] == data["conditions"]
    assert report["end_reason"] == data["outcome"]["end_reason"]
    assert report["user_decision"] == data["user_decision"]
    assert report["diagnoses"] == data["diagnoses"]
    assert report["approvals"] == data["approvals"]
    assert report["scaling_results"] == data["scaling_results"]
    assert report["revalidations"] == data["revalidations"]

    return report


# ----------------------------------------------------------------------
# RunRecorder 단위 테스트
# ----------------------------------------------------------------------


def test_initial_measurement_saves_unmeasured_resources_as_label():
    recorder = _recorder()
    recorder.set_start_replicas(1)

    recorder.observe(
        _state_with(_load_result(50.0, 3000.0), _metrics(40), _report(True))
    )

    [record] = recorder.measurement_history

    assert record["round"] == 1
    assert record["phase"] == "initial"
    assert record["replicas"] == 1
    assert record["virtual_users"] == 50
    assert record["duration"] == 10
    assert record["tps"] == 50.0
    assert record["latency_p95"] == 3000.0
    assert record["connection_count"] == 40
    # 0.0 고정값을 숫자로 저장하지 않는다 (ISSUE-10)
    assert record["cpu_pct"] == UNMEASURED_LABEL
    assert record["mem_pct"] == UNMEASURED_LABEL
    assert record["timestamp"] == "2026-09-11T03:00:00+00:00"


def test_zero_replicas_is_saved_as_null():
    recorder = _recorder()
    recorder.set_start_replicas(0)

    recorder.observe(_state_with(_load_result(50.0, 3000.0), _metrics(40)))

    assert recorder.conditions["start_replicas"] is None
    assert recorder.measurement_history[0]["replicas"] is None


def test_observe_same_objects_does_not_duplicate():
    recorder = _recorder()
    state = _state_with(
        _load_result(50.0, 3000.0),
        _metrics(40),
        _report(True),
    )

    recorder.observe(state)
    recorder.observe(state)

    record = recorder.build_record(state, "failed")

    assert len(record["measurement_history"]) == 1
    assert len(record["diagnoses"]) == 1


def test_after_scaling_round_without_recollected_metrics_has_null_connection():
    recorder = _recorder()
    recorder.set_start_replicas(1)

    state = _state_with(
        _load_result(50.0, 3000.0),
        _metrics(40),
        _report(True),
    )
    recorder.observe(state)

    # 스케일링 후 재측정은 됐지만 메트릭 수집이 실패해 이전 SystemMetrics 객체가 남은 경우
    state["scaling_result"] = ScalingResult(
        before_replicas=1,
        after_replicas=2,
        success=True,
    )
    state["load_test_result"] = _load_result(98.0, 980.0)
    recorder.observe(state)

    first, second = recorder.measurement_history

    assert (first["phase"], first["replicas"]) == ("initial", 1)
    assert (second["phase"], second["replicas"]) == ("after_scaling", 2)
    assert second["connection_count"] is None
    assert second["timestamp"] is None


def test_conditions_record_slo_model_and_virtual_users(monkeypatch):
    monkeypatch.setenv("P95_SLO_MS", "1200")

    conditions = _recorder().conditions

    assert conditions["virtual_users"] == 50
    assert conditions["duration_sec"] == 10
    assert conditions["p95_slo_ms"] == 1200
    assert conditions["llm_model"] == nodes.UPSTAGE_MODEL
    assert conditions["resource_metrics_collected"] is False
    assert conditions["force_scaling_requested"] is True
    assert conditions["debug_endpoints_enabled"] is False


def test_invalid_slo_is_recorded_as_null(monkeypatch):
    monkeypatch.setenv("P95_SLO_MS", "abc")

    assert _recorder().conditions["p95_slo_ms"] is None


def test_finalize_writes_one_file_and_is_idempotent(results_dir):
    recorder = _recorder("task-abc")
    state = _state_with(_load_result(50.0, 3000.0), _metrics(40))

    path = recorder.finalize(state, "no_scaling_proposed", "✅ 완료")

    assert path is not None
    assert re.fullmatch(r"\d{8}_\d{6}_task-abc\.json", path.name)
    assert recorder.finalize(state, "stream_closed") == path
    assert len(list(results_dir.glob("*.json"))) == 1
    assert not list(results_dir.glob("*.tmp"))

    data = json.loads(path.read_text(encoding="utf-8"))

    assert data["task_id"] == "task-abc"
    assert data["outcome"]["end_reason"] == "no_scaling_proposed"
    assert data["outcome"]["final_message"] == "✅ 완료"


def test_save_failure_returns_none_without_raising(tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("디렉터리가 아닌 파일", encoding="utf-8")
    monkeypatch.setattr(run_history, "RESULTS_DIR", blocker / "results")

    recorder = _recorder()
    state = _state_with(_load_result(50.0, 3000.0), _metrics(40))

    assert recorder.finalize(state, "failed") is None
    assert recorder.result_file is None


def test_revalidation_parse_failure_keeps_raw_text():
    recorder = _recorder()
    state = _state_with(_load_result(50.0, 3000.0), _metrics(40))
    recorder.observe(state)

    recorder.capture_revalidation("JSON이 아닌 응답")
    state["load_test_result"] = _load_result(98.0, 980.0)
    recorder.observe(state)

    [revalidation] = recorder.build_record(state, "failed")["revalidations"]

    assert revalidation["round"] == 2
    assert "parse_error" in revalidation
    assert revalidation["raw"] == "JSON이 아닌 응답"


def test_read_locust_task_weights(tmp_path):
    locustfile = tmp_path / "locustfile.py"
    locustfile.write_text(
        "from locust import HttpUser, task\n"
        "\n"
        "class U(HttpUser):\n"
        "    @task(6)\n"
        "    def light(self):\n"
        '        self.client.get("/light", name="/light")\n'
        "\n"
        "    @task\n"
        "    def health(self):\n"
        '        self.client.get("/health")\n'
        "\n"
        "    def helper(self):\n"
        '        self.client.get("/not-a-task")\n',
        encoding="utf-8",
    )

    assert read_locust_task_weights(locustfile) == {
        "/light": 6,
        "/health": 1,
    }


def test_unreadable_locustfile_gives_null(tmp_path):
    broken = tmp_path / "locustfile.py"
    broken.write_text("def (", encoding="utf-8")

    assert read_locust_task_weights(broken) is None
    assert read_locust_task_weights(tmp_path / "missing.py") is None


# ----------------------------------------------------------------------
# start_agent_stream 경로별 테스트
# ----------------------------------------------------------------------


async def test_stream_no_scaling_proposed_saves_file(
    results_dir,
    fake_infra,
    monkeypatch,
):
    monkeypatch.setattr(
        agent,
        "run_diagnosis",
        _fake_diagnosis(requires_scaling=False),
    )

    events = await _run_stream()

    assert events[-1]["status"] == "done"

    data = _only_result_file(results_dir)

    assert data["task_id"] == events[-1]["task_id"]
    assert data["conditions"]["start_replicas"] == 1
    assert [
        (m["round"], m["phase"], m["replicas"])
        for m in data["measurement_history"]
    ] == [(1, "initial", 1)]
    assert data["diagnoses"][0]["requires_scaling"] is False
    assert data["approvals"] == []
    assert data["user_decision"] == "not_applicable"
    assert data["forced_scaling"] is False
    assert data["outcome"]["agent_outcome"] == "diagnosed"
    assert data["outcome"]["end_reason"] == "no_scaling_proposed"

    report = await _assert_report_matches_file(data)

    assert report["end_reason"] == "no_scaling_proposed"
    assert report["user_decision"] == "not_applicable"
    assert report["conditions"]["p95_slo_ms"] == 1000
    assert report["conditions"]["virtual_users"] == 50


async def test_stream_approved_scaling_records_two_rounds_and_report(
    results_dir,
    fake_infra,
    monkeypatch,
):
    monkeypatch.setattr(
        agent,
        "run_diagnosis",
        _fake_diagnosis(requires_scaling=True),
    )
    monkeypatch.setattr(agent, "resume_after_approval", _fake_resume())

    events = await _run_stream(decision=True)
    statuses = [event["status"] for event in events]

    assert "need_approval" in statuses
    assert statuses[-1] == "done"

    data = _only_result_file(results_dir)
    history = data["measurement_history"]

    assert [(m["round"], m["phase"], m["replicas"]) for m in history] == [
        (1, "initial", 1),
        (2, "after_scaling", 2),
    ]
    assert history[1]["tps"] == 98.0
    assert history[1]["connection_count"] == 12
    assert data["approvals"] == [
        {
            "round": 1,
            "source": "llm",
            "scaling_plan": {"current_replicas": 1, "desired_replicas": 2},
            # 신뢰도 0.8 (기준 이상) → 확인 게이트 없음 (#83)
            "low_confidence": False,
            "acknowledged": None,
            "decision": "approved",
        }
    ]
    assert data["scaling_results"] == [
        {
            "round": 1,
            "before_replicas": 1,
            "after_replicas": 2,
            "success": True,
            "error_message": None,
        }
    ]

    [revalidation] = data["revalidations"]

    assert revalidation["round"] == 2
    assert revalidation["summary"] == "재검증 요약"
    assert revalidation["performance_improved"] is True
    # LLM이 계산한 변화량은 측정값이 아니므로 저장하지 않는다
    assert "tps_change" not in revalidation

    assert data["user_decision"] == "approved"
    assert data["outcome"]["agent_outcome"] == "scaled"
    assert data["outcome"]["end_reason"] == "scaled"

    report = await _assert_report_matches_file(data)

    assert report["end_reason"] == "scaled"
    assert report["user_decision"] == "approved"
    assert report["scaling_results"][0]["after_replicas"] == 2
    assert report["measurement"]["tps"] == history[0]["tps"]
    assert report["measurement_after"]["tps"] == history[1]["tps"]
    assert report["forced_scaling"] is False
    assert report["result_file"] is not None


async def test_stream_rejected_keeps_diagnosed_outcome(
    results_dir,
    fake_infra,
    monkeypatch,
):
    monkeypatch.setattr(
        agent,
        "run_diagnosis",
        _fake_diagnosis(requires_scaling=True),
    )
    monkeypatch.setattr(agent, "resume_after_approval", _fake_resume())

    events = await _run_stream(decision=False)

    # 거절 시 SSE status는 기존 동작(failed) 그대로다
    assert events[-1]["status"] == "failed"

    data = _only_result_file(results_dir)

    assert len(data["measurement_history"]) == 1
    assert data["approvals"][0]["decision"] == "rejected"
    assert data["scaling_results"] == []
    assert data["user_decision"] == "rejected"
    assert data["outcome"]["agent_outcome"] == "diagnosed"
    assert data["outcome"]["end_reason"] == "rejected"

    report = await _assert_report_matches_file(data)

    # 거절이면 state outcome은 diagnosed지만 /report의 end_reason·user_decision으로 구분된다
    assert report["outcome"] == "diagnosed"
    assert report["end_reason"] == "rejected"
    assert report["user_decision"] == "rejected"
    assert report["approvals"][0]["scaling_plan"] == {
        "current_replicas": 1,
        "desired_replicas": 2,
    }


@pytest.mark.parametrize(
    ("fail_before_load", "expected_rounds"),
    [(True, 0), (False, 1)],
)
async def test_stream_failed_diagnosis_saves_file(
    results_dir,
    fake_infra,
    monkeypatch,
    fail_before_load,
    expected_rounds,
):
    monkeypatch.setattr(
        agent,
        "run_diagnosis",
        _fake_diagnosis(
            fail_before_load=fail_before_load,
            fail_after_load=not fail_before_load,
        ),
    )

    events = await _run_stream()

    assert events[-1]["status"] == "failed"

    data = _only_result_file(results_dir)

    assert len(data["measurement_history"]) == expected_rounds
    assert data["outcome"]["agent_outcome"] == "failed"
    assert data["outcome"]["end_reason"] == "failed"
    assert data["outcome"]["error"]

    report = await _assert_report_matches_file(data)

    assert report["end_reason"] == "failed"
    assert report["error"] == data["outcome"]["error"]


async def test_stream_infra_check_failure_saves_file(results_dir, monkeypatch):
    def unhealthy_client(*args, **kwargs):
        return _REAL_ASYNC_CLIENT(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(503)
            )
        )

    monkeypatch.setattr(agent.httpx, "AsyncClient", unhealthy_client)

    events = await _run_stream()

    assert events[-1]["status"] == "failed"

    data = _only_result_file(results_dir)

    assert data["measurement_history"] == []
    assert data["conditions"]["start_replicas"] is None
    assert data["outcome"]["end_reason"] == "failed"
    assert "503" in data["outcome"]["final_message"]


async def test_stream_forced_scaling_is_recorded(
    results_dir,
    fake_infra,
    monkeypatch,
):
    monkeypatch.setattr(agent, "DEBUG_ENDPOINTS_ENABLED", True)
    monkeypatch.setattr(
        agent,
        "run_diagnosis",
        _fake_diagnosis(requires_scaling=False),
    )
    monkeypatch.setattr(agent, "resume_after_approval", _fake_resume())

    await _run_stream(decision=True, force_scaling=True)

    data = _only_result_file(results_dir)

    assert data["forced_scaling"] is True
    assert data["conditions"]["debug_endpoints_enabled"] is True
    assert data["approvals"][0]["source"] == "forced"
    # LLM 원본 판단은 그대로 남는다
    assert data["diagnoses"][0]["requires_scaling"] is False
    assert data["outcome"]["end_reason"] == "scaled"

    report = await _assert_report_matches_file(data)

    assert report["forced_scaling"] is True
    assert report["approvals"][0]["source"] == "forced"


async def test_stream_force_request_without_debug_is_not_forced(
    results_dir,
    fake_infra,
    monkeypatch,
):
    monkeypatch.setattr(
        agent,
        "run_diagnosis",
        _fake_diagnosis(requires_scaling=False),
    )

    events = await _run_stream(force_scaling=True)

    assert events[-1]["status"] == "done"

    data = _only_result_file(results_dir)

    assert data["conditions"]["force_scaling_requested"] is True
    assert data["forced_scaling"] is False
    assert data["outcome"]["end_reason"] == "no_scaling_proposed"


async def test_stream_continues_when_result_file_cannot_be_saved(
    tmp_path,
    fake_infra,
    monkeypatch,
):
    blocker = tmp_path / "blocker"
    blocker.write_text("디렉터리가 아닌 파일", encoding="utf-8")
    monkeypatch.setattr(run_history, "RESULTS_DIR", blocker / "results")
    monkeypatch.setattr(
        agent,
        "run_diagnosis",
        _fake_diagnosis(requires_scaling=False),
    )

    events = await _run_stream()

    assert events[-1]["status"] == "done"

    report = await agent.get_report(events[-1]["task_id"])

    assert report["result_file"] is None
    assert len(report["measurement_history"]) == 1


async def test_stream_closed_while_waiting_for_approval_saves_file(
    results_dir,
    fake_infra,
    monkeypatch,
):
    monkeypatch.setattr(
        agent,
        "run_diagnosis",
        _fake_diagnosis(requires_scaling=True),
    )

    stream = agent.start_agent_stream(target_tps=50, duration=10)

    async for frame in stream:
        event = _parse(frame)
        if event["status"] == "need_approval":
            break

    # 실행이 끝나기 전 /report: end_reason·result_file은 아직 없고 승인 요청은 결정 전이다
    report = await agent.get_report(event["task_id"])

    assert report["waiting_for_approval"] is True
    assert report["end_reason"] is None
    assert report["result_file"] is None
    assert report["approvals"][0]["decision"] is None

    # 승인 대기 중 클라이언트가 연결을 끊은 경우
    await stream.aclose()

    data = _only_result_file(results_dir)

    assert data["approvals"][0]["decision"] is None
    assert data["user_decision"] == "no_response"
    assert data["outcome"]["agent_outcome"] == "awaiting_approval"
    assert data["outcome"]["end_reason"] == "stream_closed"


# ----------------------------------------------------------------------
# 저신뢰 확인 게이트 (#83): confidence < LOW_CONFIDENCE_THRESHOLD(0.6)인 스케일링 제안.
# 실측 confidence는 80~95%라 UI로는 확인하기 어려워 단위 테스트로 검증한다 (docs/02 ISSUE-13).
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("requires_scaling", "confidence", "expected"),
    [
        (True, 0.59, True),
        (True, 0.6, False),    # 경계값은 게이트 대상이 아니다
        (True, 0.95, False),
        (False, 0.3, False),   # 스케일링 제안이 아니면 게이트 없음 (forced 덮어쓰기 포함)
    ],
)
def test_is_low_confidence(requires_scaling, confidence, expected):
    state = _state_with(
        _load_result(50.0, 3000.0),
        _metrics(40),
        _report(requires_scaling, confidence),
    )

    assert agent.LOW_CONFIDENCE_THRESHOLD == 0.6
    assert agent._is_low_confidence(state) is expected


def test_is_low_confidence_without_report():
    assert agent._is_low_confidence(_state_with(None, None)) is False


async def test_low_confidence_approval_requires_acknowledgement(
    results_dir,
    fake_infra,
    monkeypatch,
):
    monkeypatch.setattr(
        agent,
        "run_diagnosis",
        _fake_diagnosis(requires_scaling=True, confidence=0.59),
    )
    monkeypatch.setattr(agent, "resume_after_approval", _fake_resume())

    events = []

    async for frame in agent.start_agent_stream(target_tps=50, duration=10):
        event = _parse(frame)
        events.append(event)

        if event["status"] != "need_approval":
            continue

        task_id = event["task_id"]

        assert event["low_confidence"] is True
        assert event["confidence"] == 0.59
        assert event["low_confidence_threshold"] == 0.6

        report = await agent.get_report(task_id)

        assert report["low_confidence"] is True
        assert report["waiting_for_approval"] is True

        # 확인 플래그 없는 승인은 API에서 400. 승인 대기는 그대로 남는다 (UI 우회 방지)
        with pytest.raises(HTTPException) as exc_info:
            await agent.approve_task(
                agent.ApprovalRequest(task_id=task_id, approved=True)
            )

        assert exc_info.value.status_code == 400
        assert not task_manager.futures[task_id].done()

        response = await agent.approve_task(
            agent.ApprovalRequest(
                task_id=task_id,
                approved=True,
                acknowledge_low_confidence=True,
            )
        )

        assert response["status"] == "success"

    assert events[-1]["status"] == "done"

    data = _only_result_file(results_dir)
    [approval] = data["approvals"]

    assert approval["low_confidence"] is True
    assert approval["acknowledged"] is True
    assert approval["decision"] == "approved"
    assert data["outcome"]["end_reason"] == "scaled"

    report = await _assert_report_matches_file(data)

    assert report["low_confidence"] is True
    assert report["low_confidence_threshold"] == 0.6


async def test_confidence_at_threshold_has_no_gate(
    results_dir,
    fake_infra,
    monkeypatch,
):
    monkeypatch.setattr(
        agent,
        "run_diagnosis",
        _fake_diagnosis(requires_scaling=True, confidence=0.6),
    )
    monkeypatch.setattr(agent, "resume_after_approval", _fake_resume())

    # _run_stream은 확인 플래그 없이 승인한다 → 경계값 0.6은 기존처럼 바로 승인된다
    events = await _run_stream(decision=True)
    [need_approval] = [e for e in events if e["status"] == "need_approval"]

    assert need_approval["low_confidence"] is False
    assert events[-1]["status"] == "done"

    data = _only_result_file(results_dir)
    [approval] = data["approvals"]

    assert approval["low_confidence"] is False
    assert approval["acknowledged"] is None
    assert approval["decision"] == "approved"

    report = await _assert_report_matches_file(data)

    assert report["low_confidence"] is False


async def test_low_confidence_rejection_needs_no_acknowledgement(
    results_dir,
    fake_infra,
    monkeypatch,
):
    monkeypatch.setattr(
        agent,
        "run_diagnosis",
        _fake_diagnosis(requires_scaling=True, confidence=0.59),
    )
    monkeypatch.setattr(agent, "resume_after_approval", _fake_resume())

    # _run_stream은 확인 플래그 없이 거절한다 → 거절은 항상 받는다
    events = await _run_stream(decision=False)

    assert events[-1]["status"] == "failed"

    data = _only_result_file(results_dir)
    [approval] = data["approvals"]

    assert approval["low_confidence"] is True
    assert approval["acknowledged"] is False
    assert approval["decision"] == "rejected"
    assert data["outcome"]["end_reason"] == "rejected"


def test_approve_endpoint_returns_400_over_http():
    """HTTP 경로로도 확인 플래그 없는 저신뢰 승인은 400이고 승인 대기가 유지된다."""

    loop = asyncio.new_event_loop()

    try:
        task_id = "low-confidence-task"
        task_manager.states[task_id] = create_initial_state(target_tps=50, duration=10)
        task_manager.futures[task_id] = loop.create_future()
        task_manager.low_confidence[task_id] = True

        response = TestClient(app).post(
            "/api/v1/agent/approve",
            json={"task_id": task_id, "approved": True},
        )

        assert response.status_code == 400
        assert "acknowledge_low_confidence" in response.json()["detail"]
        assert not task_manager.futures[task_id].done()
    finally:
        loop.close()


# ----------------------------------------------------------------------
# 결과 패널 그래프용 데이터 (docs/03 Phase 4 그래프)
# 초 단위 시계열·엔드포인트 통계(부하 테스트와 함께)와 서버별 요청 수(부하 뒤 백그라운드).
# 아래 값은 테스트 입력값이며 측정값이 아니다.
# ----------------------------------------------------------------------

GRAPH_DETAILS = {
    "timeseries": {
        "bucket_sec": 1,
        "source": "locust_request_log",
        "percentile": "nearest_rank_p95",
        "points": [{"t": 0, "requests": 40, "failures": 0, "p95_ms": 2900.0}],
    },
    "endpoints": [
        {
            "name": "/heavy",
            "method": "GET",
            "requests": 120,
            "failures": 0,
            "error_rate": 0.0,
            "p95_ms": 4100.0,
            "avg_ms": 1500.0,
            "rps": 12.0,
        }
    ],
}

INSTANCE_BEFORE = {"172.18.0.3:8080": 1000.0}
INSTANCE_AFTER = {"172.18.0.3:8080": 1420.0}


def _fake_diagnosis_using_load_node(requires_scaling: bool = False):
    """agent.py가 넘긴 load_test_node(그래프 데이터 수집 노드)를 engine처럼 호출하는 가짜 진단."""

    async def fake_run_diagnosis(state, load_test_node=None, **kwargs):
        state.update(await load_test_node(state))

        if state["agent_outcome"] == "failed":
            return state

        state["system_metrics"] = _metrics(40)
        state["bottleneck_report"] = _report(requires_scaling)
        state["loop_count"] += 1
        state.update(agent_outcome="diagnosed", final_answer="진단 완료: 병목 없음")
        return state

    return fake_run_diagnosis


@pytest.fixture
def fake_load_test(monkeypatch):
    """run_load_test_detailed와 인스턴스별 요청 카운터(부하 전 → 부하 후 순서)를 가짜로 둔다."""

    snapshots = [dict(INSTANCE_BEFORE), dict(INSTANCE_AFTER)]

    def fake_detailed(target_tps, duration):
        return _load_result(50.0, 3000.0), json.loads(json.dumps(GRAPH_DETAILS))

    async def fake_counts():
        return snapshots.pop(0) if snapshots else None

    monkeypatch.setattr(agent, "run_load_test_detailed", fake_detailed)
    monkeypatch.setattr(agent, "get_request_counts_by_instance", fake_counts)
    monkeypatch.setattr(agent, "INSTANCE_SNAPSHOT_DELAY_SEC", 0.0)
    return snapshots


def test_measurement_record_without_details_has_null_graph_fields():
    """그래프 데이터가 붙지 않은 측정(예: 가짜 부하 테스트)은 세 필드가 null이다."""

    recorder = _recorder()
    recorder.observe(_state_with(_load_result(50.0, 3000.0), _metrics(40), _report(False)))

    [record] = recorder.measurement_history

    assert record["timeseries"] is None
    assert record["endpoints"] is None
    assert record["requests_by_instance"] is None


def test_late_instance_requests_update_existing_record():
    """서버별 요청 수는 측정 레코드가 만들어진 뒤에 도착해도 그 레코드에 붙는다."""

    recorder = _recorder()
    load = _load_result(50.0, 3000.0)
    entry = recorder.attach_load_test_details(load, json.loads(json.dumps(GRAPH_DETAILS)))

    recorder.observe(_state_with(load, _metrics(40), _report(False)))

    [record] = recorder.measurement_history

    assert record["timeseries"] == GRAPH_DETAILS["timeseries"]
    assert record["endpoints"] == GRAPH_DETAILS["endpoints"]
    assert record["requests_by_instance"] is None

    recorder.set_instance_requests(entry, {"counts": {"172.18.0.3:8080": 420}})

    assert recorder.measurement_history[0]["requests_by_instance"] == {
        "counts": {"172.18.0.3:8080": 420}
    }


async def test_stream_saves_graph_data_with_measurement(
    results_dir,
    fake_infra,
    fake_load_test,
    monkeypatch,
):
    monkeypatch.setattr(agent, "run_diagnosis", _fake_diagnosis_using_load_node())

    events = await _run_stream()

    assert events[-1]["status"] == "done"

    data = _only_result_file(results_dir)
    [record] = data["measurement_history"]

    # LoadTestResult 값은 그대로다
    assert record["tps"] == 50.0
    assert record["latency_p95"] == 3000.0
    assert record["timeseries"] == GRAPH_DETAILS["timeseries"]
    assert record["endpoints"] == GRAPH_DETAILS["endpoints"]
    assert record["requests_by_instance"] == {
        "handlers": ["/light", "/heavy", "/flaky"],
        "excluded": ["/health", "/metrics"],
        "counts": {"172.18.0.3:8080": 420},
    }

    await _assert_report_matches_file(data)


async def test_stream_saves_null_when_instance_snapshot_is_late(
    results_dir,
    fake_infra,
    fake_load_test,
    monkeypatch,
):
    """부하 뒤 서버별 조회가 기다리는 시간보다 늦으면 null로 저장하고 흐름은 끝까지 간다."""

    calls = []

    async def slow_after_counts():
        calls.append(1)
        if len(calls) == 1:
            return dict(INSTANCE_BEFORE)
        await asyncio.sleep(5)
        return dict(INSTANCE_AFTER)

    monkeypatch.setattr(agent, "get_request_counts_by_instance", slow_after_counts)
    monkeypatch.setattr(agent, "DETAILS_SETTLE_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(agent, "run_diagnosis", _fake_diagnosis_using_load_node())

    events = await _run_stream()

    assert events[-1]["status"] == "done"

    [record] = _only_result_file(results_dir)["measurement_history"]

    assert record["timeseries"] == GRAPH_DETAILS["timeseries"]
    assert record["requests_by_instance"] is None


async def test_stream_continues_when_graph_collection_fails(
    results_dir,
    fake_infra,
    monkeypatch,
):
    """Prometheus 조회가 예외를 던지고 Locust 추가 데이터가 없어도 부하 테스트 결과와 흐름은 그대로다."""

    def fake_detailed(target_tps, duration):
        return _load_result(50.0, 3000.0), {"timeseries": None, "endpoints": None}

    async def broken_counts():
        raise RuntimeError("prometheus down")

    monkeypatch.setattr(agent, "run_load_test_detailed", fake_detailed)
    monkeypatch.setattr(agent, "get_request_counts_by_instance", broken_counts)
    monkeypatch.setattr(agent, "INSTANCE_SNAPSHOT_DELAY_SEC", 0.0)
    monkeypatch.setattr(agent, "run_diagnosis", _fake_diagnosis_using_load_node())

    events = await _run_stream()

    assert events[-1]["status"] == "done"

    [record] = _only_result_file(results_dir)["measurement_history"]

    assert record["tps"] == 50.0
    assert record["timeseries"] is None
    assert record["endpoints"] is None
    assert record["requests_by_instance"] is None


async def test_stream_load_test_failure_through_capture_node(
    results_dir,
    fake_infra,
    monkeypatch,
):
    """수집 노드를 거쳐도 Locust 실패는 기존과 같이 실패 경로로 끝난다."""

    def failing_detailed(target_tps, duration):
        raise LoadTestError("Locust 결과 파일을 찾을 수 없습니다")

    async def counts():
        return {}

    monkeypatch.setattr(agent, "run_load_test_detailed", failing_detailed)
    monkeypatch.setattr(agent, "get_request_counts_by_instance", counts)
    monkeypatch.setattr(agent, "run_diagnosis", _fake_diagnosis_using_load_node())

    events = await _run_stream()

    assert events[-1]["status"] == "failed"

    data = _only_result_file(results_dir)

    assert data["measurement_history"] == []
    assert data["outcome"]["end_reason"] == "failed"
    assert "부하 테스트 실행 실패" in data["outcome"]["error"]


async def test_stream_records_headline_source(
    results_dir,
    fake_infra,
    monkeypatch,
):
    """결과 파일 측정 레코드에 헤드라인·엔드포인트 통계의 계산 출처가 남는다 (#85, docs/02 ISSUE-15)."""

    details = json.loads(json.dumps(GRAPH_DETAILS))
    details.update(headline_source="locust_final_stats", endpoints_source="locust_final_stats")

    def fake_detailed(target_tps, duration):
        return _load_result(50.0, 3000.0), details

    async def counts():
        return {}

    monkeypatch.setattr(agent, "run_load_test_detailed", fake_detailed)
    monkeypatch.setattr(agent, "get_request_counts_by_instance", counts)
    monkeypatch.setattr(agent, "INSTANCE_SNAPSHOT_DELAY_SEC", 0.0)
    monkeypatch.setattr(agent, "run_diagnosis", _fake_diagnosis_using_load_node())

    events = await _run_stream()

    assert events[-1]["status"] == "done"

    [record] = _only_result_file(results_dir)["measurement_history"]

    assert record["headline_source"] == "locust_final_stats"
    assert record["endpoints_source"] == "locust_final_stats"
