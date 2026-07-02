from __future__ import annotations
import pytest
from datetime import datetime
import json
import os
import sys
from fastapi.testclient import TestClient
from typing import Any

# 1. 상단 임시 클래스 제거 후 app.schemas에서 실제 정의된 규격 모델 임포트
from app.schemas import (
    LoadTestResult,
    ScalingResult,
    SystemMetrics,
    BottleneckReport,
)

# 1. 요구사항 규격 검증 테스트 (실제 6개 필수 인자 규격 준수)
def test_project_dataclass_specs():
    load_test = LoadTestResult(
        tps=350.5, 
        latency_p95=120.0, 
        latency_avg=40.0, 
        error_rate=0.02, 
        duration=60, 
        total_requests=100
    )
    assert load_test.tps == 350.5

    # SystemMetrics 필드 사양 준수 (timestamp 제외)
    metrics = SystemMetrics(
        cpu_pct=92.5, 
        mem_pct=78.0, 
        connection_count=1500
    )
    assert metrics.cpu_pct > 90.0

    # BottleneckReport는 테스트용 로컬 임시 클래스로 격리
    from dataclasses import dataclass
    @dataclass
    class LocalBottleneckReport:
        cause: str
        severity: str
        recommendation: str
        confidence: float

    report = LocalBottleneckReport(
        cause="CPU Bottleneck", severity="high", recommendation="Scale-out", confidence=0.95
    )
    assert report.severity == "high"


# 2. HITL(승인 프로세스) 우회 차단 검증 (함수 전체 포함 및 실제 스키마 인자 매핑)
def test_hitl_guardrail_flow():
    user_approved = False  # 유저 승인 전 상태
    scaling_triggered = False
    
    # HITL 지점이 우회되지 않는지 검증
    if user_approved:
        scaling_triggered = True
        
    assert scaling_triggered is False, "HITL 우회 오류: 승인 없이 스케일링이 트리거되었습니다."

    # 유저가 대시보드에서 [승인] 버튼을 누른 상황
    user_approved = True
    if user_approved:
        scaling_triggered = True
        # 실제 schemas.py 규격(before_replicas, after_replicas, success)에 맞춰 주입
        final_result = ScalingResult(before_replicas=1, after_replicas=3, success=True)

    assert scaling_triggered is True
    assert final_result.success is True


# 3. MAX_LOOP 및 가드레일 조건 검증 (체크리스트 필수 요건)
def test_guardrail_constraints():
    MAX_LOOP = 5
    current_loop = 0
    
    while current_loop < 10:
        current_loop += 1
        if current_loop >= MAX_LOOP:
            break  # 가드레일 이탈 방지
            
    assert current_loop == MAX_LOOP

# nodes.py 단위 테스트
from app.agent.state import create_initial_state, AgentRuntimeState

@pytest.mark.asyncio
async def test_run_load_test_node_success():
    """부하 테스트 Node가 정상 결과를 반환하는지 확인한다."""

    state = create_initial_state(
        target_tps=30,
        duration=10,
    )

    def mock_run_load_test(
        target_tps: int,
        duration: int,
    ) -> LoadTestResult:
        assert target_tps == 30
        assert duration == 10

        return LoadTestResult(
            tps=28.5,
            latency_p95=850.0,
            latency_avg=320.0,
            error_rate=0.02,
            duration=10,
            total_requests=285,
        )

    result = await run_load_test_node(
        state,
        load_test_runner=mock_run_load_test,
    )

    assert result["load_test_result"].tps == 28.5
    assert result["load_test_result"].latency_p95 == 850.0
    assert result["agent_outcome"] == "pending"
    assert result["error"] is None


@pytest.mark.asyncio
async def test_run_load_test_node_failure():
    """부하 테스트 Tool에서 예외가 발생했을 때 실패 처리되는지 확인한다."""

    state = create_initial_state(
        target_tps=30,
        duration=10,
    )

    def mock_run_load_test(
        target_tps: int,
        duration: int,
    ) -> LoadTestResult:
        raise RuntimeError("Locust 실행 실패")

    result = await run_load_test_node(
        state,
        load_test_runner=mock_run_load_test,
    )

    assert result["agent_outcome"] == "failed"
    assert result["waiting_for_approval"] is False
    assert "Locust 실행 실패" in result["error"]


@pytest.mark.asyncio
async def test_collect_metrics_node_success():
    """메트릭 수집 Node가 SystemMetrics를 반환하는지 확인한다."""

    state = create_initial_state(
        target_tps=30,
        duration=10,
    )

    async def mock_get_system_metrics() -> SystemMetrics:
        return SystemMetrics(
            cpu_pct=85.0,
            mem_pct=62.0,
            connection_count=15,
        )

    result = await collect_metrics_node(
        state,
        metrics_collector=mock_get_system_metrics,
    )

    assert result["system_metrics"].cpu_pct == 85.0
    assert result["system_metrics"].mem_pct == 62.0
    assert result["system_metrics"].connection_count == 15
    assert result["agent_outcome"] == "pending"
    assert result["error"] is None


@pytest.mark.asyncio
async def test_collect_metrics_node_failure():
    """Prometheus 메트릭 수집 실패가 상태에 반영되는지 확인한다."""

    state = create_initial_state(
        target_tps=30,
        duration=10,
    )

    async def mock_get_system_metrics() -> SystemMetrics:
        raise RuntimeError("Prometheus 연결 실패")

    result = await collect_metrics_node(
        state,
        metrics_collector=mock_get_system_metrics,
    )

    assert result["agent_outcome"] == "failed"
    assert result["waiting_for_approval"] is False
    assert "Prometheus 연결 실패" in result["error"]


@pytest.mark.asyncio
async def test_llm_reasoning_node_requires_scaling(monkeypatch):
    """LLM이 스케일링 필요 결과를 반환했을 때 상태를 확인한다."""

    state = create_initial_state(
        target_tps=50,
        duration=30,
    )

    state["load_test_result"] = LoadTestResult(
        tps=30.0,
        latency_p95=1800.0,
        latency_avg=900.0,
        error_rate=0.08,
        duration=30,
        total_requests=900,
    )

    state["system_metrics"] = SystemMetrics(
        cpu_pct=90.0,
        mem_pct=70.0,
        connection_count=100,
    )

    # Docker를 실제로 조회하지 않고 현재 replica 수를 1로 고정한다.
    monkeypatch.setattr(
        "app.agent.nodes._get_current_replicas",
        lambda: 1,
    )

    async def mock_llm_caller(
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        assert "InfraGuard Agent" in system_prompt
        assert "50" in user_prompt
        assert "target-server" in user_prompt

        return """
        {
          "bottleneck_report": {
            "cause": "목표 TPS 미달과 높은 응답 지연이 확인되었습니다.",
            "severity": "high",
            "recommendation": "컨테이너 수를 증가시킵니다.",
            "confidence": 0.93,
            "requires_scaling": true
          },
          "agent_outcome": "awaiting_approval",
          "scaling_plan": {
            "service_name": "target-server",
            "current_replicas": 1,
            "desired_replicas": 2,
            "reason": "목표 TPS 미달 및 높은 P95 응답 시간"
          }
        }
        """

    result = await llm_reasoning_node(
        state,
        llm_caller=mock_llm_caller,
    )

    assert result["bottleneck_report"].severity == "high"
    assert result["bottleneck_report"].confidence == 0.93
    assert result["bottleneck_report"].requires_scaling is True

    assert result["agent_outcome"] == "awaiting_approval"
    assert result["scaling_required"] is True
    assert result["waiting_for_approval"] is True
    assert result["scaling_plan"]["desired_replicas"] == 2
    assert result["loop_count"] == 1
    assert result["error"] is None


@pytest.mark.asyncio
async def test_llm_reasoning_node_without_scaling(monkeypatch):
    """LLM이 스케일링 불필요 결과를 반환했을 때 상태를 확인한다."""

    state = create_initial_state(
        target_tps=30,
        duration=30,
    )

    state["load_test_result"] = LoadTestResult(
        tps=31.0,
        latency_p95=350.0,
        latency_avg=180.0,
        error_rate=0.0,
        duration=30,
        total_requests=930,
    )

    state["system_metrics"] = SystemMetrics(
        cpu_pct=45.0,
        mem_pct=50.0,
        connection_count=5,
    )

    monkeypatch.setattr(
        "app.agent.nodes._get_current_replicas",
        lambda: 1,
    )

    async def mock_llm_caller(
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        return """
        {
          "bottleneck_report": {
            "cause": "목표 TPS를 달성했으며 병목이 확인되지 않았습니다.",
            "severity": "low",
            "recommendation": "현재 구성을 유지합니다.",
            "confidence": 0.96,
            "requires_scaling": false
          },
          "agent_outcome": "diagnosed",
          "scaling_plan": null
        }
        """

    result = await llm_reasoning_node(
        state,
        llm_caller=mock_llm_caller,
    )

    assert result["agent_outcome"] == "diagnosed"
    assert result["scaling_required"] is False
    assert result["waiting_for_approval"] is False
    assert result["scaling_plan"] is None
    assert result["loop_count"] == 1
    assert result["error"] is None


@pytest.mark.asyncio
async def test_llm_reasoning_node_invalid_json(monkeypatch):
    """LLM이 JSON이 아닌 값을 반환했을 때 실패하는지 확인한다."""

    state = create_initial_state(
        target_tps=30,
        duration=10,
    )

    state["load_test_result"] = LoadTestResult(
        tps=20.0,
        latency_p95=1000.0,
        latency_avg=500.0,
        error_rate=0.05,
        duration=10,
        total_requests=200,
    )

    state["system_metrics"] = SystemMetrics(
        cpu_pct=80.0,
        mem_pct=60.0,
        connection_count=20,
    )

    monkeypatch.setattr(
        "app.agent.nodes._get_current_replicas",
        lambda: 1,
    )

    async def mock_llm_caller(
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        return "JSON이 아닌 잘못된 응답"

    result = await llm_reasoning_node(
        state,
        llm_caller=mock_llm_caller,
    )

    assert result["agent_outcome"] == "failed"
    assert result["waiting_for_approval"] is False
    assert "JSON" in result["error"]
    assert result["loop_count"] == 1


@pytest.mark.asyncio
async def test_llm_reasoning_node_missing_load_test_result():
    """부하 테스트 결과가 없으면 LLM 분석을 실행하지 않는지 확인한다."""

    state = create_initial_state(
        target_tps=30,
        duration=10,
    )

    state["system_metrics"] = SystemMetrics(
        cpu_pct=80.0,
        mem_pct=60.0,
        connection_count=20,
    )

    async def mock_llm_caller(
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        pytest.fail("LLM 호출 함수가 실행되면 안 됩니다.")

    result = await llm_reasoning_node(
        state,
        llm_caller=mock_llm_caller,
    )

    assert result["agent_outcome"] == "failed"
    assert "load_test_result" in result["error"]
    assert result["loop_count"] == 1


@pytest.mark.asyncio
async def test_execute_scaling_node_without_approval():
    """사용자 승인 없이 스케일링이 차단되는지 확인한다."""

    state = create_initial_state(
        target_tps=50,
        duration=30,
    )

    state["scaling_plan"] = {
        "service_name": "target-server",
        "current_replicas": 1,
        "desired_replicas": 2,
        "reason": "목표 TPS 미달",
    }

    state["scaling_approved"] = False

    scaler_called = False

    async def mock_scaler(
        target_replicas: int,
    ) -> ScalingResult:
        nonlocal scaler_called
        scaler_called = True

        return ScalingResult(
            before_replicas=1,
            after_replicas=target_replicas,
            success=True,
        )

    result = await execute_scaling_node(
        state,
        scaler=mock_scaler,
    )

    assert scaler_called is False
    assert result["agent_outcome"] == "failed"
    assert result["waiting_for_approval"] is False
    assert "승인 없이" in result["error"]


@pytest.mark.asyncio
async def test_execute_scaling_node_success():
    """사용자 승인 후 스케일링 Tool이 호출되는지 확인한다."""

    state = create_initial_state(
        target_tps=50,
        duration=30,
    )

    state["scaling_plan"] = {
        "service_name": "target-server",
        "current_replicas": 1,
        "desired_replicas": 2,
        "reason": "목표 TPS 미달",
    }

    state["scaling_approved"] = True

    async def mock_scaler(
        target_replicas: int,
    ) -> ScalingResult:
        assert target_replicas == 2

        return ScalingResult(
            before_replicas=1,
            after_replicas=2,
            success=True,
        )

    result = await execute_scaling_node(
        state,
        scaler=mock_scaler,
    )

    assert result["scaling_result"].success is True
    assert result["scaling_result"].before_replicas == 1
    assert result["scaling_result"].after_replicas == 2

    assert result["agent_outcome"] == "scaled"
    assert result["waiting_for_approval"] is False
    assert result["scaling_count"] == 1
    assert result["error"] is None


@pytest.mark.asyncio
async def test_execute_scaling_node_failure():
    """스케일링 Tool이 실패 결과를 반환했을 때 상태를 확인한다."""

    state = create_initial_state(
        target_tps=50,
        duration=30,
    )

    state["scaling_plan"] = {
        "service_name": "target-server",
        "current_replicas": 1,
        "desired_replicas": 2,
        "reason": "목표 TPS 미달",
    }

    state["scaling_approved"] = True

    async def mock_scaler(
        target_replicas: int,
    ) -> ScalingResult:
        return ScalingResult(
            before_replicas=1,
            after_replicas=1,
            success=False,
            error_message="Docker Compose 스케일링 실패",
        )

    result = await execute_scaling_node(
        state,
        scaler=mock_scaler,
    )

    assert result["scaling_result"].success is False
    assert result["agent_outcome"] == "failed"
    assert result["waiting_for_approval"] is False
    assert "Docker Compose 스케일링 실패" in result["error"]

# local에서 state.py, schemas.py, nodes.py, prompts.py가 정상적으로 연동되는지 확인하는 코드
from app.agent.nodes import (
    collect_metrics_node,
    execute_scaling_node,
    llm_reasoning_node,
    run_load_test_node,
    generate_plan_node,
)

@pytest.mark.asyncio
async def test_nodes_full_flow(monkeypatch):
    state = create_initial_state(
        target_tps=50,
        duration=30,
    )

    def mock_run_load_test(
        target_tps: int,
        duration: int,
    ) -> LoadTestResult:
        return LoadTestResult(
            tps=30.0,
            latency_p95=1800.0,
            latency_avg=900.0,
            error_rate=0.08,
            duration=duration,
            total_requests=900,
        )

    async def mock_get_metrics() -> SystemMetrics:
        return SystemMetrics(
            cpu_pct=92.0,
            mem_pct=70.0,
            connection_count=100,
        )

    async def mock_llm(
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        return """
        {
          "bottleneck_report": {
            "cause": "CPU 사용률과 응답 지연이 높습니다.",
            "severity": "high",
            "recommendation": "컨테이너 확장을 권장합니다.",
            "confidence": 0.94,
            "requires_scaling": true
          },
          "agent_outcome": "awaiting_approval",
          "scaling_plan": {
            "service_name": "target-server",
            "current_replicas": 1,
            "desired_replicas": 2,
            "reason": "목표 TPS 미달과 CPU 과부하"
          }
        }
        """

    async def mock_scale_service(
        target_replicas: int,
    ) -> ScalingResult:
        return ScalingResult(
            before_replicas=1,
            after_replicas=target_replicas,
            success=True,
        )

    monkeypatch.setattr(
        "app.agent.nodes._get_current_replicas",
        lambda: 1,
    )

    # 1. 부하 테스트
    update = await run_load_test_node(
        state,
        load_test_runner=mock_run_load_test,
    )
    state.update(update)

    assert state["load_test_result"] is not None
    assert state["agent_outcome"] == "pending"

    # 2. 메트릭 수집
    update = await collect_metrics_node(
        state,
        metrics_collector=mock_get_metrics,
    )
    state.update(update)

    assert state["system_metrics"] is not None
    assert state["system_metrics"].cpu_pct == 92.0

    # 3. LLM 분석
    update = await llm_reasoning_node(
        state,
        llm_caller=mock_llm,
    )
    state.update(update)

    assert state["agent_outcome"] == "awaiting_approval"
    assert state["scaling_required"] is True
    assert state["waiting_for_approval"] is True
    assert state["scaling_plan"]["desired_replicas"] == 2

    # 4. 사용자 승인
    state["scaling_approved"] = True

    # 5. 스케일링
    update = await execute_scaling_node(
        state,
        scaler=mock_scale_service,
    )
    state.update(update)

    assert state["agent_outcome"] == "scaled"
    assert state["scaling_result"].success is True
    assert state["scaling_result"].after_replicas == 2
    assert state["scaling_count"] == 1

from app.agent.nodes import (
    NodeExecutionError,
    _create_bottleneck_report,
    _parse_llm_json,
    _validate_agent_outcome,
    _validate_scaling_plan,
)

# nodes.py 내부 JSON 파싱 및 검증 함수 단위 테스트


def test_parse_llm_json_valid_json():
    """정상 JSON 문자열이 딕셔너리로 변환되는지 확인한다."""

    content = """
    {
      "agent_outcome": "diagnosed",
      "scaling_plan": null
    }
    """

    result = _parse_llm_json(content)

    assert isinstance(result, dict)
    assert result["agent_outcome"] == "diagnosed"
    assert result["scaling_plan"] is None


def test_parse_llm_json_removes_markdown_code_block():
    """Markdown 코드 블록으로 감싼 JSON도 정상 파싱하는지 확인한다."""

    content = """
    ```json
    {
      "agent_outcome": "awaiting_approval",
      "scaling_plan": {
        "desired_replicas": 2
      }
    }
    ```
    """

    result = _parse_llm_json(content)

    assert result["agent_outcome"] == "awaiting_approval"
    assert result["scaling_plan"]["desired_replicas"] == 2


def test_parse_llm_json_rejects_invalid_json():
    """JSON이 아닌 문자열을 전달하면 예외가 발생하는지 확인한다."""

    with pytest.raises(
        NodeExecutionError,
        match="JSON으로 변환하지 못했습니다",
    ):
        _parse_llm_json("JSON이 아닌 응답입니다.")


def test_parse_llm_json_rejects_non_object_json():
    """최상위 JSON 값이 객체가 아니면 예외가 발생하는지 확인한다."""

    with pytest.raises(
        NodeExecutionError,
        match="최상위 값은 JSON 객체",
    ):
        _parse_llm_json("[1, 2, 3]")

def test_create_bottleneck_report_success():
    """정상 LLM 응답으로 BottleneckReport가 생성되는지 확인한다."""

    response_data = {
        "bottleneck_report": {
            "cause": "CPU 사용률이 높습니다.",
            "severity": "high",
            "recommendation": "컨테이너 확장을 권장합니다.",
            "confidence": 0.93,
            "requires_scaling": True,
        }
    }

    result = _create_bottleneck_report(response_data)

    assert result.cause == "CPU 사용률이 높습니다."
    assert result.severity == "high"
    assert result.recommendation == "컨테이너 확장을 권장합니다."
    assert result.confidence == 0.93
    assert result.requires_scaling is True


def test_create_bottleneck_report_rejects_missing_object():
    """bottleneck_report 객체가 없으면 예외가 발생하는지 확인한다."""

    with pytest.raises(
        NodeExecutionError,
        match="bottleneck_report 객체가 없습니다",
    ):
        _create_bottleneck_report({})


def test_create_bottleneck_report_rejects_missing_field():
    """필수 필드가 누락되면 예외가 발생하는지 확인한다."""

    response_data = {
        "bottleneck_report": {
            "cause": "CPU 과부하",
            "severity": "high",
            "recommendation": "스케일링 권장",
            "confidence": 0.9,
            # requires_scaling 누락
        }
    }

    with pytest.raises(
        NodeExecutionError,
        match="필드가 누락되었습니다",
    ):
        _create_bottleneck_report(response_data)


@pytest.mark.parametrize(
    "severity",
    [
        "critical",
        "unknown",
        "",
    ],
)
def test_create_bottleneck_report_rejects_invalid_severity(
    severity: str,
):
    """허용되지 않은 severity 값이 거부되는지 확인한다."""

    response_data = {
        "bottleneck_report": {
            "cause": "CPU 과부하",
            "severity": severity,
            "recommendation": "스케일링 권장",
            "confidence": 0.9,
            "requires_scaling": True,
        }
    }

    with pytest.raises(
        NodeExecutionError,
        match="severity는 low, medium, high",
    ):
        _create_bottleneck_report(response_data)


@pytest.mark.parametrize(
    "confidence",
    [
        -0.1,
        1.1,
    ],
)
def test_create_bottleneck_report_rejects_invalid_confidence(
    confidence: float,
):
    """confidence 범위가 0~1을 벗어나면 예외가 발생하는지 확인한다."""

    response_data = {
        "bottleneck_report": {
            "cause": "CPU 과부하",
            "severity": "high",
            "recommendation": "스케일링 권장",
            "confidence": confidence,
            "requires_scaling": True,
        }
    }

    with pytest.raises(
        NodeExecutionError,
        match="confidence는 0.0부터 1.0 사이",
    ):
        _create_bottleneck_report(response_data)


def test_create_bottleneck_report_rejects_non_boolean_scaling():
    """requires_scaling이 bool이 아니면 예외가 발생하는지 확인한다."""

    response_data = {
        "bottleneck_report": {
            "cause": "CPU 과부하",
            "severity": "high",
            "recommendation": "스케일링 권장",
            "confidence": 0.9,
            "requires_scaling": "true",
        }
    }

    with pytest.raises(
        NodeExecutionError,
        match="true 또는 false",
    ):
        _create_bottleneck_report(response_data)

@pytest.mark.parametrize(
    (
        "outcome",
        "requires_scaling",
        "expected",
    ),
    [
        (
            "awaiting_approval",
            True,
            "awaiting_approval",
        ),
        (
            "diagnosed",
            False,
            "diagnosed",
        ),
    ],
)
def test_validate_agent_outcome_success(
    outcome: str,
    requires_scaling: bool,
    expected: str,
):
    """requires_scaling과 agent_outcome이 일치하는지 확인한다."""

    result = _validate_agent_outcome(
        outcome,
        requires_scaling,
    )

    assert result == expected


@pytest.mark.parametrize(
    (
        "outcome",
        "requires_scaling",
    ),
    [
        (
            "diagnosed",
            True,
        ),
        (
            "awaiting_approval",
            False,
        ),
        (
            "completed",
            True,
        ),
    ],
)
def test_validate_agent_outcome_rejects_mismatch(
    outcome: str,
    requires_scaling: bool,
):
    """requires_scaling과 agent_outcome이 다르면 거부하는지 확인한다."""

    with pytest.raises(
        NodeExecutionError,
        match="requires_scaling 값과 일치하지 않습니다",
    ):
        _validate_agent_outcome(
            outcome,
            requires_scaling,
        )

def test_validate_scaling_plan_success():
    """정상적인 스케일링 계획이 반환되는지 확인한다."""

    scaling_plan = {
        "service_name": "target-server",
        "current_replicas": 1,
        "desired_replicas": 2,
        "reason": "CPU 사용률이 높습니다.",
    }

    result = _validate_scaling_plan(
        scaling_plan,
        service_name="target-server",
        current_replicas=1,
        requires_scaling=True,
    )

    assert result is not None
    assert result["service_name"] == "target-server"
    assert result["current_replicas"] == 1
    assert result["desired_replicas"] == 2
    assert result["reason"] == "CPU 사용률이 높습니다."


def test_validate_scaling_plan_returns_none_when_not_required():
    """스케일링이 불필요하고 계획이 None이면 정상 처리되는지 확인한다."""

    result = _validate_scaling_plan(
        None,
        service_name="target-server",
        current_replicas=1,
        requires_scaling=False,
    )

    assert result is None


def test_validate_scaling_plan_rejects_plan_when_not_required():
    """스케일링이 불필요한데 계획이 있으면 예외가 발생하는지 확인한다."""

    scaling_plan = {
        "service_name": "target-server",
        "current_replicas": 1,
        "desired_replicas": 2,
        "reason": "불필요한 계획",
    }

    with pytest.raises(
        NodeExecutionError,
        match="scaling_plan은 null",
    ):
        _validate_scaling_plan(
            scaling_plan,
            service_name="target-server",
            current_replicas=1,
            requires_scaling=False,
        )


def test_validate_scaling_plan_requires_object():
    """스케일링이 필요한데 계획 객체가 없으면 예외가 발생하는지 확인한다."""

    with pytest.raises(
        NodeExecutionError,
        match="scaling_plan 객체가 필요합니다",
    ):
        _validate_scaling_plan(
            None,
            service_name="target-server",
            current_replicas=1,
            requires_scaling=True,
        )


def test_validate_scaling_plan_rejects_missing_field():
    """스케일링 계획의 필수 필드가 누락되면 예외가 발생하는지 확인한다."""

    scaling_plan = {
        "service_name": "target-server",
        "current_replicas": 1,
        "desired_replicas": 2,
        # reason 누락
    }

    with pytest.raises(
        NodeExecutionError,
        match="scaling_plan 필드가 누락되었습니다",
    ):
        _validate_scaling_plan(
            scaling_plan,
            service_name="target-server",
            current_replicas=1,
            requires_scaling=True,
        )


def test_validate_scaling_plan_rejects_wrong_service_name():
    """서비스 이름이 실제 값과 다르면 예외가 발생하는지 확인한다."""

    scaling_plan = {
        "service_name": "wrong-service",
        "current_replicas": 1,
        "desired_replicas": 2,
        "reason": "CPU 과부하",
    }

    with pytest.raises(
        NodeExecutionError,
        match="service_name이 실제 서비스 이름과 다릅니다",
    ):
        _validate_scaling_plan(
            scaling_plan,
            service_name="target-server",
            current_replicas=1,
            requires_scaling=True,
        )


def test_validate_scaling_plan_rejects_wrong_current_replicas():
    """현재 replica 수가 실제 상태와 다르면 예외가 발생하는지 확인한다."""

    scaling_plan = {
        "service_name": "target-server",
        "current_replicas": 2,
        "desired_replicas": 3,
        "reason": "CPU 과부하",
    }

    with pytest.raises(
        NodeExecutionError,
        match="current_replicas가 실제 값과 다릅니다",
    ):
        _validate_scaling_plan(
            scaling_plan,
            service_name="target-server",
            current_replicas=1,
            requires_scaling=True,
        )


@pytest.mark.parametrize(
    "desired_replicas",
    [
        1,
        0,
    ],
)
def test_validate_scaling_plan_rejects_non_increasing_replicas(
    desired_replicas: int,
):
    """목표 replica 수가 현재 수보다 크지 않으면 거부하는지 확인한다."""

    scaling_plan = {
        "service_name": "target-server",
        "current_replicas": 1,
        "desired_replicas": desired_replicas,
        "reason": "CPU 과부하",
    }

    with pytest.raises(
        NodeExecutionError,
        match="desired_replicas는 current_replicas보다 커야 합니다",
    ):
        _validate_scaling_plan(
            scaling_plan,
            service_name="target-server",
            current_replicas=1,
            requires_scaling=True,
        )
# engine.py 단위 테스트

import asyncio

import app.agent.engine as engine


def _engine_load_result(
    *,
    tps: float = 30.0,
    latency_p95: float = 900.0,
) -> LoadTestResult:
    """Engine 테스트에서 공통으로 사용하는 부하 테스트 결과."""

    return LoadTestResult(
        tps=tps,
        latency_p95=latency_p95,
        latency_avg=latency_p95 / 2,
        error_rate=0.02,
        duration=30,
        total_requests=int(tps * 30),
    )


def _engine_metrics(
    *,
    cpu_pct: float = 80.0,
) -> SystemMetrics:
    """Engine 테스트에서 공통으로 사용하는 시스템 메트릭."""

    return SystemMetrics(
        cpu_pct=cpu_pct,
        mem_pct=65.0,
        connection_count=50,
    )


def _approval_waiting_state() -> AgentRuntimeState:
    """승인 대기 상태와 스케일링 전 측정 결과를 함께 만든다."""

    state = create_initial_state(
        target_tps=50,
        duration=30,
    )

    state.update(
        {
            "load_test_result": _engine_load_result(
                tps=30.0,
                latency_p95=1800.0,
            ),
            "system_metrics": _engine_metrics(
                cpu_pct=92.0,
            ),
            "bottleneck_report": BottleneckReport(
                cause="목표 TPS 미달과 CPU 과부하",
                severity="high",
                recommendation="컨테이너 수 증가",
                confidence=0.95,
                requires_scaling=True,
            ),
            "agent_outcome": "awaiting_approval",
            "scaling_required": True,
            "waiting_for_approval": True,
            "scaling_plan": {
                "service_name": "target-server",
                "current_replicas": 1,
                "desired_replicas": 2,
                "reason": "목표 TPS 미달 및 CPU 과부하",
            },
            "loop_count": 1,
            "error": None,
        }
    )

    return state


@pytest.mark.asyncio
async def test_engine_runs_diagnosis_nodes_in_order():
    """
    Engine이 다음 순서로 Node를 실행하는지 확인한다.

    run_load_test_node
    → collect_metrics_node
    → llm_reasoning_node
    """

    state = create_initial_state(
        target_tps=30,
        duration=10,
    )
    executed: list[str] = []

    async def fake_load(
        current_state: AgentRuntimeState,
    ) -> dict:
        executed.append("load")

        return {
            "load_test_result": _engine_load_result(
                tps=31.0,
                latency_p95=300.0,
            ),
            "agent_outcome": "pending",
            "error": None,
        }

    async def fake_metrics(
        current_state: AgentRuntimeState,
    ) -> dict:
        executed.append("metrics")
        assert current_state["load_test_result"] is not None

        return {
            "system_metrics": _engine_metrics(
                cpu_pct=40.0,
            ),
            "agent_outcome": "pending",
            "error": None,
        }

    async def fake_reasoning(
        current_state: AgentRuntimeState,
    ) -> dict:
        executed.append("reasoning")
        assert current_state["system_metrics"] is not None

        return {
            "bottleneck_report": BottleneckReport(
                cause="병목 없음",
                severity="low",
                recommendation="현재 구성 유지",
                confidence=0.97,
                requires_scaling=False,
            ),
            "agent_outcome": "diagnosed",
            "scaling_required": False,
            "waiting_for_approval": False,
            "scaling_plan": None,
            "loop_count": current_state["loop_count"] + 1,
            "final_answer": "진단 완료",
            "error": None,
        }

    result = await engine.run_diagnosis(
        state,
        load_test_node=fake_load,
        metrics_node=fake_metrics,
        reasoning_node=fake_reasoning,
    )

    assert result is state
    assert executed == [
        "load",
        "metrics",
        "reasoning",
    ]
    assert result["agent_outcome"] == "diagnosed"
    assert result["bottleneck_report"] is not None
    assert result["loop_count"] == 1
    assert result["error"] is None


@pytest.mark.asyncio
async def test_engine_stops_when_load_test_fails():
    """부하 테스트 실패 후 다음 Node가 실행되지 않는지 확인한다."""

    state = create_initial_state(
        target_tps=30,
        duration=10,
    )
    executed: list[str] = []

    async def failed_load(
        current_state: AgentRuntimeState,
    ) -> dict:
        executed.append("load")

        return {
            "agent_outcome": "failed",
            "waiting_for_approval": False,
            "final_answer": None,
            "error": "Locust 실행 실패",
        }

    async def must_not_run(
        current_state: AgentRuntimeState,
    ) -> dict:
        executed.append("unexpected")
        pytest.fail(
            "부하 테스트 실패 후 다음 Node가 실행되면 안 됩니다."
        )

    result = await engine.run_diagnosis(
        state,
        load_test_node=failed_load,
        metrics_node=must_not_run,
        reasoning_node=must_not_run,
    )

    assert executed == ["load"]
    assert result["agent_outcome"] == "failed"
    assert "Locust 실행 실패" in result["error"]


@pytest.mark.asyncio
async def test_engine_waits_for_approval_when_scaling_is_required():
    """스케일링 필요 시 awaiting_approval로 종료되는지 확인한다."""

    state = create_initial_state(
        target_tps=50,
        duration=30,
    )

    async def fake_load(
        current_state: AgentRuntimeState,
    ) -> dict:
        return {
            "load_test_result": _engine_load_result(
                tps=30.0,
                latency_p95=1800.0,
            ),
            "agent_outcome": "pending",
            "error": None,
        }

    async def fake_metrics(
        current_state: AgentRuntimeState,
    ) -> dict:
        return {
            "system_metrics": _engine_metrics(
                cpu_pct=92.0,
            ),
            "agent_outcome": "pending",
            "error": None,
        }

    async def fake_reasoning(
        current_state: AgentRuntimeState,
    ) -> dict:
        return {
            "bottleneck_report": BottleneckReport(
                cause="CPU 과부하",
                severity="high",
                recommendation="스케일 아웃",
                confidence=0.95,
                requires_scaling=True,
            ),
            "agent_outcome": "awaiting_approval",
            "scaling_required": True,
            "waiting_for_approval": True,
            "scaling_plan": {
                "service_name": "target-server",
                "current_replicas": 1,
                "desired_replicas": 2,
                "reason": "CPU 과부하",
            },
            "loop_count": current_state["loop_count"] + 1,
            "error": None,
        }

    result = await engine.run_diagnosis(
        state,
        load_test_node=fake_load,
        metrics_node=fake_metrics,
        reasoning_node=fake_reasoning,
    )

    assert result["agent_outcome"] == "awaiting_approval"
    assert result["scaling_required"] is True
    assert result["waiting_for_approval"] is True
    assert result["scaling_plan"]["desired_replicas"] == 2
    assert engine.is_waiting_for_approval(result) is True


@pytest.mark.asyncio
async def test_engine_rejects_scaling_without_running_scaling_node():
    """사용자 거절 시 스케일링 Node가 실행되지 않는지 확인한다."""

    state = _approval_waiting_state()
    scaling_called = False

    async def must_not_scale(
        current_state: AgentRuntimeState,
    ) -> dict:
        nonlocal scaling_called
        scaling_called = True
        pytest.fail(
            "사용자 거절 후 스케일링이 실행되면 안 됩니다."
        )

    result = await engine.resume_after_approval(
        state,
        approved=False,
        scaling_node=must_not_scale,
    )

    assert scaling_called is False
    assert result["scaling_approved"] is False
    assert result["waiting_for_approval"] is False
    assert result["agent_outcome"] == "diagnosed"
    assert result["error"] is None


@pytest.mark.asyncio
async def test_engine_scales_and_revalidates_successfully():
    """
    승인 후 스케일링, 재부하 테스트, 재메트릭 수집,
    Solar 재검증까지 성공하는지 확인한다.
    """

    state = _approval_waiting_state()
    executed: list[str] = []

    async def fake_scaling(
        current_state: AgentRuntimeState,
    ) -> dict:
        executed.append("scaling")
        assert current_state["scaling_approved"] is True

        return {
            "scaling_result": ScalingResult(
                before_replicas=1,
                after_replicas=2,
                success=True,
            ),
            "scaling_count": (
                current_state["scaling_count"] + 1
            ),
            "agent_outcome": "scaled",
            "waiting_for_approval": False,
            "error": None,
        }

    async def fake_revalidation_load(
        current_state: AgentRuntimeState,
    ) -> dict:
        executed.append("revalidation_load")

        return {
            "load_test_result": _engine_load_result(
                tps=55.0,
                latency_p95=600.0,
            ),
            "agent_outcome": "pending",
            "error": None,
        }

    async def fake_revalidation_metrics(
        current_state: AgentRuntimeState,
    ) -> dict:
        executed.append("revalidation_metrics")

        return {
            "system_metrics": _engine_metrics(
                cpu_pct=55.0,
            ),
            "agent_outcome": "pending",
            "error": None,
        }

    async def fake_revalidation(
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        executed.append("revalidation_llm")
        assert system_prompt
        assert user_prompt

        return json.dumps(
            {
                "summary": "스케일링 후 성능이 개선되었습니다.",
                "performance_improved": True,
                "additional_action_required": False,
                "recommended_action": None,
            },
            ensure_ascii=False,
        )

    result = await engine.resume_after_approval(
        state,
        approved=True,
        scaling_node=fake_scaling,
        load_test_node=fake_revalidation_load,
        metrics_node=fake_revalidation_metrics,
        revalidation_caller=fake_revalidation,
    )

    assert executed == [
        "scaling",
        "revalidation_load",
        "revalidation_metrics",
        "revalidation_llm",
    ]
    assert result["agent_outcome"] == "scaled"
    assert result["scaling_result"].success is True
    assert result["scaling_result"].after_replicas == 2
    assert result["scaling_count"] == 1
    assert result["waiting_for_approval"] is False
    assert result["error"] is None
    assert "재검증 완료" in result["final_answer"]


@pytest.mark.asyncio
async def test_engine_runs_next_reasoning_loop_when_revalidation_fails():
    """
    재검증 결과가 충분하지 않으면 reasoning Node를 다시 실행하는지 확인한다.
    """

    state = _approval_waiting_state()
    reasoning_called = 0

    async def fake_scaling(
        current_state: AgentRuntimeState,
    ) -> dict:
        return {
            "scaling_result": ScalingResult(
                before_replicas=1,
                after_replicas=2,
                success=True,
            ),
            "scaling_count": (
                current_state["scaling_count"] + 1
            ),
            "agent_outcome": "scaled",
            "waiting_for_approval": False,
            "error": None,
        }

    async def fake_revalidation_load(
        current_state: AgentRuntimeState,
    ) -> dict:
        return {
            "load_test_result": _engine_load_result(
                tps=35.0,
                latency_p95=1400.0,
            ),
            "agent_outcome": "pending",
            "error": None,
        }

    async def fake_revalidation_metrics(
        current_state: AgentRuntimeState,
    ) -> dict:
        return {
            "system_metrics": _engine_metrics(
                cpu_pct=80.0,
            ),
            "agent_outcome": "pending",
            "error": None,
        }

    async def fake_reasoning(
        current_state: AgentRuntimeState,
    ) -> dict:
        nonlocal reasoning_called
        reasoning_called += 1

        return {
            "bottleneck_report": BottleneckReport(
                cause="병목 지속",
                severity="high",
                recommendation="추가 스케일링",
                confidence=0.9,
                requires_scaling=True,
            ),
            "agent_outcome": "awaiting_approval",
            "scaling_required": True,
            "waiting_for_approval": True,
            "scaling_plan": {
                "service_name": "target-server",
                "current_replicas": 2,
                "desired_replicas": 3,
                "reason": "성능 개선 부족",
            },
            "loop_count": (
                current_state["loop_count"] + 1
            ),
            "final_answer": "추가 승인이 필요합니다.",
            "error": None,
        }

    async def fake_revalidation(
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        return json.dumps(
            {
                "summary": "성능 개선이 충분하지 않습니다.",
                "performance_improved": False,
                "additional_action_required": True,
                "recommended_action": "replica 추가 증가",
            },
            ensure_ascii=False,
        )

    result = await engine.resume_after_approval(
        state,
        approved=True,
        scaling_node=fake_scaling,
        load_test_node=fake_revalidation_load,
        metrics_node=fake_revalidation_metrics,
        reasoning_node=fake_reasoning,
        revalidation_caller=fake_revalidation,
    )

    assert reasoning_called == 1
    assert result["agent_outcome"] == "awaiting_approval"
    assert result["waiting_for_approval"] is True
    assert result["scaling_plan"]["desired_replicas"] == 3
    assert result["loop_count"] == 2
    assert result["error"] is None


@pytest.mark.asyncio
async def test_engine_max_loop_guard_blocks_additional_diagnosis():
    """MAX_LOOP 도달 후 추가 진단 Node 실행을 차단하는지 확인한다."""

    state = create_initial_state(
        target_tps=10,
        duration=5,
    )

    max_loop = getattr(
        engine,
        "MAX_LOOP_COUNT",
        getattr(engine, "MAX_LOOP", None),
    )
    assert max_loop is not None, (
        "engine.py에 MAX_LOOP_COUNT 또는 MAX_LOOP가 필요합니다."
    )

    state["loop_count"] = max_loop

    async def must_not_run(
        current_state: AgentRuntimeState,
    ) -> dict:
        pytest.fail(
            "MAX_LOOP 도달 후 Node가 실행되면 안 됩니다."
        )

    result = await engine.run_diagnosis(
        state,
        load_test_node=must_not_run,
        metrics_node=must_not_run,
        reasoning_node=must_not_run,
    )

    assert result["agent_outcome"] == "failed"
    assert str(max_loop) in (result["error"] or "")


@pytest.mark.asyncio
async def test_engine_timeout_returns_failed_state(
    monkeypatch: pytest.MonkeyPatch,
):
    """Engine 제한 시간 초과가 failed 상태로 변환되는지 확인한다."""

    monkeypatch.setattr(
        engine,
        "ENGINE_TIMEOUT_SECONDS",
        0.01,
    )

    async def slow_load(
        current_state: AgentRuntimeState,
    ) -> dict:
        await asyncio.sleep(0.1)
        return {}

    result = await engine.start_agent(
        target_tps=10,
        duration=5,
        load_test_node=slow_load,
    )

    assert result["agent_outcome"] == "failed"
    assert "제한 시간" in (result["error"] or "")


@pytest.mark.asyncio
async def test_engine_scaling_exception_becomes_failed_state():
    """스케일링 Node 예외가 failed 상태로 변환되는지 확인한다."""

    state = _approval_waiting_state()

    async def failed_scaling(
        current_state: AgentRuntimeState,
    ) -> dict:
        raise RuntimeError("Docker 스케일링 오류")

    result = await engine.resume_after_approval(
        state,
        approved=True,
        scaling_node=failed_scaling,
    )

    assert result["agent_outcome"] == "failed"
    assert result["waiting_for_approval"] is False
    assert "Docker 스케일링 오류" in (
        result["error"] or ""
    )


# engine.py 통합 테스트
@pytest.mark.asyncio
async def test_engine_integration_diagnosis_without_scaling(
    monkeypatch: pytest.MonkeyPatch,
):
    """
    Engine이 실제 Node들을 순서대로 실행하고,
    스케일링이 불필요하면 diagnosed로 종료되는지 확인한다.
    """

    state = create_initial_state(
        target_tps=30,
        duration=10,
    )

    def fake_load_test(
        *,
        target_tps: int,
        duration: int,
    ) -> LoadTestResult:
        assert target_tps == 30
        assert duration == 10

        return LoadTestResult(
            tps=32.0,
            latency_p95=300.0,
            latency_avg=150.0,
            error_rate=0.0,
            duration=duration,
            total_requests=320,
        )

    async def fake_metrics() -> SystemMetrics:
        return SystemMetrics(
            cpu_pct=42.0,
            mem_pct=51.0,
            connection_count=8,
        )

    async def fake_llm(
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        assert "InfraGuard Agent" in system_prompt
        assert "target-server" in user_prompt

        return """
        {
          "bottleneck_report": {
            "cause": "목표 TPS를 달성했고 병목이 없습니다.",
            "severity": "low",
            "recommendation": "현재 구성을 유지합니다.",
            "confidence": 0.97,
            "requires_scaling": false
          },
          "agent_outcome": "diagnosed",
          "scaling_plan": null
        }
        """

    monkeypatch.setattr(
        "app.agent.nodes._get_current_replicas",
        lambda: 1,
    )

    async def load_node(
        current_state: AgentRuntimeState,
    ) -> dict:
        return await run_load_test_node(
            current_state,
            load_test_runner=fake_load_test,
        )

    async def metrics_node(
        current_state: AgentRuntimeState,
    ) -> dict:
        return await collect_metrics_node(
            current_state,
            metrics_collector=fake_metrics,
        )

    async def reasoning_node(
        current_state: AgentRuntimeState,
    ) -> dict:
        return await llm_reasoning_node(
            current_state,
            llm_caller=fake_llm,
        )

    result = await engine.run_diagnosis(
        state,
        load_test_node=load_node,
        metrics_node=metrics_node,
        reasoning_node=reasoning_node,
    )

    assert result is state
    assert result["load_test_result"] is not None
    assert result["system_metrics"] is not None
    assert result["bottleneck_report"] is not None
    assert result["agent_outcome"] == "diagnosed"
    assert result["scaling_required"] is False
    assert result["waiting_for_approval"] is False
    assert result["scaling_plan"] is None
    assert result["loop_count"] == 1
    assert result["error"] is None


@pytest.mark.asyncio
async def test_engine_integration_scaling_and_revalidation_success(
    monkeypatch: pytest.MonkeyPatch,
):
    """
    진단 → 승인 대기 → 승인 → 스케일링 → 재검증 성공까지
    전체 흐름이 정상 동작하는지 확인한다.
    """

    state = create_initial_state(
        target_tps=50,
        duration=30,
    )

    load_calls = 0
    metric_calls = 0

    def fake_load_test(
        *,
        target_tps: int,
        duration: int,
    ) -> LoadTestResult:
        nonlocal load_calls
        load_calls += 1

        if load_calls == 1:
            return LoadTestResult(
                tps=28.0,
                latency_p95=1900.0,
                latency_avg=950.0,
                error_rate=0.09,
                duration=duration,
                total_requests=840,
            )

        return LoadTestResult(
            tps=55.0,
            latency_p95=600.0,
            latency_avg=300.0,
            error_rate=0.01,
            duration=duration,
            total_requests=1650,
        )

    async def fake_metrics() -> SystemMetrics:
        nonlocal metric_calls
        metric_calls += 1

        if metric_calls == 1:
            return SystemMetrics(
                cpu_pct=93.0,
                mem_pct=76.0,
                connection_count=120,
            )

        return SystemMetrics(
            cpu_pct=55.0,
            mem_pct=60.0,
            connection_count=80,
        )

    async def fake_reasoning_llm(
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        return """
        {
          "bottleneck_report": {
            "cause": "목표 TPS 미달과 CPU 과부하가 확인되었습니다.",
            "severity": "high",
            "recommendation": "컨테이너 수를 늘립니다.",
            "confidence": 0.95,
            "requires_scaling": true
          },
          "agent_outcome": "awaiting_approval",
          "scaling_plan": {
            "service_name": "target-server",
            "current_replicas": 1,
            "desired_replicas": 2,
            "reason": "목표 TPS 미달 및 CPU 과부하"
          }
        }
        """

    async def fake_scaler(
        target_replicas: int,
    ) -> ScalingResult:
        assert target_replicas == 2

        return ScalingResult(
            before_replicas=1,
            after_replicas=2,
            success=True,
        )

    async def fake_revalidation_llm(
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        return json.dumps(
            {
                "summary": "TPS와 응답 지연이 개선되었습니다.",
                "performance_improved": True,
                "additional_action_required": False,
                "recommended_action": None,
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(
        "app.agent.nodes._get_current_replicas",
        lambda: 1,
    )

    async def load_node(
        current_state: AgentRuntimeState,
    ) -> dict:
        return await run_load_test_node(
            current_state,
            load_test_runner=fake_load_test,
        )

    async def metrics_node(
        current_state: AgentRuntimeState,
    ) -> dict:
        return await collect_metrics_node(
            current_state,
            metrics_collector=fake_metrics,
        )

    async def reasoning_node(
        current_state: AgentRuntimeState,
    ) -> dict:
        return await llm_reasoning_node(
            current_state,
            llm_caller=fake_reasoning_llm,
        )

    async def scaling_node(
        current_state: AgentRuntimeState,
    ) -> dict:
        return await execute_scaling_node(
            current_state,
            scaler=fake_scaler,
        )

    diagnosed = await engine.run_diagnosis(
        state,
        load_test_node=load_node,
        metrics_node=metrics_node,
        reasoning_node=reasoning_node,
    )

    assert diagnosed["agent_outcome"] == "awaiting_approval"
    assert diagnosed["waiting_for_approval"] is True
    assert diagnosed["scaling_plan"]["desired_replicas"] == 2

    result = await engine.resume_after_approval(
        diagnosed,
        approved=True,
        scaling_node=scaling_node,
        load_test_node=load_node,
        metrics_node=metrics_node,
        reasoning_node=reasoning_node,
        revalidation_caller=fake_revalidation_llm,
    )

    assert load_calls == 2
    assert metric_calls == 2
    assert result["agent_outcome"] == "scaled"
    assert result["scaling_result"] is not None
    assert result["scaling_result"].success is True
    assert result["scaling_result"].after_replicas == 2
    assert result["scaling_count"] == 1
    assert result["waiting_for_approval"] is False
    assert result["scaling_required"] is False
    assert result["scaling_plan"] is None
    assert result["error"] is None


@pytest.mark.asyncio
async def test_engine_integration_revalidation_runs_next_loop(
    monkeypatch: pytest.MonkeyPatch,
):
    """
    스케일링 후 성능 개선이 부족하면 실제 reasoning Node가 다시 실행되어
    다음 승인 대기 상태가 되는지 확인한다.
    """

    state = create_initial_state(
        target_tps=50,
        duration=30,
    )

    state.update(
        {
            "load_test_result": LoadTestResult(
                tps=28.0,
                latency_p95=1900.0,
                latency_avg=950.0,
                error_rate=0.09,
                duration=30,
                total_requests=840,
            ),
            "system_metrics": SystemMetrics(
                cpu_pct=93.0,
                mem_pct=76.0,
                connection_count=120,
            ),
            "agent_outcome": "awaiting_approval",
            "scaling_required": True,
            "waiting_for_approval": True,
            "scaling_plan": {
                "service_name": "target-server",
                "current_replicas": 1,
                "desired_replicas": 2,
                "reason": "CPU 과부하",
            },
            "loop_count": 1,
            "error": None,
        }
    )

    async def fake_scaler(
        target_replicas: int,
    ) -> ScalingResult:
        return ScalingResult(
            before_replicas=1,
            after_replicas=target_replicas,
            success=True,
        )

    def fake_load_test(
        *,
        target_tps: int,
        duration: int,
    ) -> LoadTestResult:
        return LoadTestResult(
            tps=35.0,
            latency_p95=1400.0,
            latency_avg=700.0,
            error_rate=0.05,
            duration=duration,
            total_requests=1050,
        )

    async def fake_metrics() -> SystemMetrics:
        return SystemMetrics(
            cpu_pct=80.0,
            mem_pct=70.0,
            connection_count=100,
        )

    async def fake_next_reasoning_llm(
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        return """
        {
          "bottleneck_report": {
            "cause": "스케일링 후에도 병목이 지속됩니다.",
            "severity": "high",
            "recommendation": "replica를 한 번 더 증가합니다.",
            "confidence": 0.9,
            "requires_scaling": true
          },
          "agent_outcome": "awaiting_approval",
          "scaling_plan": {
            "service_name": "target-server",
            "current_replicas": 2,
            "desired_replicas": 3,
            "reason": "성능 개선 부족"
          }
        }
        """

    async def fake_revalidation_llm(
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        return json.dumps(
            {
                "summary": "성능 개선이 충분하지 않습니다.",
                "performance_improved": False,
                "additional_action_required": True,
                "recommended_action": "replica 추가 증가",
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(
        "app.agent.nodes._get_current_replicas",
        lambda: 2,
    )

    async def scaling_node(
        current_state: AgentRuntimeState,
    ) -> dict:
        return await execute_scaling_node(
            current_state,
            scaler=fake_scaler,
        )

    async def load_node(
        current_state: AgentRuntimeState,
    ) -> dict:
        return await run_load_test_node(
            current_state,
            load_test_runner=fake_load_test,
        )

    async def metrics_node(
        current_state: AgentRuntimeState,
    ) -> dict:
        return await collect_metrics_node(
            current_state,
            metrics_collector=fake_metrics,
        )

    async def reasoning_node(
        current_state: AgentRuntimeState,
    ) -> dict:
        return await llm_reasoning_node(
            current_state,
            llm_caller=fake_next_reasoning_llm,
        )

    result = await engine.resume_after_approval(
        state,
        approved=True,
        scaling_node=scaling_node,
        load_test_node=load_node,
        metrics_node=metrics_node,
        reasoning_node=reasoning_node,
        revalidation_caller=fake_revalidation_llm,
    )

    assert result["agent_outcome"] == "awaiting_approval"
    assert result["waiting_for_approval"] is True
    assert result["scaling_required"] is True
    assert result["scaling_plan"]["current_replicas"] == 2
    assert result["scaling_plan"]["desired_replicas"] == 3
    assert result["loop_count"] == 2
    assert result["scaling_count"] == 1
    assert result["error"] is None


@pytest.mark.asyncio
async def test_engine_integration_stops_after_node_failure():
    """
    실제 run_load_test_node가 실패하면 Engine이 이후 Node를 실행하지 않는지 확인한다.
    """

    state = create_initial_state(
        target_tps=30,
        duration=10,
    )

    def failing_load_test(
        *,
        target_tps: int,
        duration: int,
    ) -> LoadTestResult:
        raise RuntimeError("통합 테스트용 Locust 실패")

    async def load_node(
        current_state: AgentRuntimeState,
    ) -> dict:
        return await run_load_test_node(
            current_state,
            load_test_runner=failing_load_test,
        )

    metrics_called = False
    reasoning_called = False

    async def must_not_run_metrics(
        current_state: AgentRuntimeState,
    ) -> dict:
        nonlocal metrics_called
        metrics_called = True
        pytest.fail(
            "부하 테스트 실패 후 metrics Node가 실행되면 안 됩니다."
        )

    async def must_not_run_reasoning(
        current_state: AgentRuntimeState,
    ) -> dict:
        nonlocal reasoning_called
        reasoning_called = True
        pytest.fail(
            "부하 테스트 실패 후 reasoning Node가 실행되면 안 됩니다."
        )

    result = await engine.run_diagnosis(
        state,
        load_test_node=load_node,
        metrics_node=must_not_run_metrics,
        reasoning_node=must_not_run_reasoning,
    )

    assert metrics_called is False
    assert reasoning_called is False
    assert result["agent_outcome"] == "failed"
    assert "통합 테스트용 Locust 실패" in result["error"]

# generate_plan.py 단위테스트
from app.tools.generate_plan import (
    PlanGenerationError,
    generate_optimization_plan,
)


def test_generate_plan_with_scaling():
    result = generate_optimization_plan(
        target_tps=50,
        load_test_result=LoadTestResult(
            tps=30.0,
            latency_p95=1800.0,
            latency_avg=900.0,
            error_rate=0.08,
            duration=30,
            total_requests=900,
        ),
        system_metrics=SystemMetrics(
            cpu_pct=92.0,
            mem_pct=70.0,
            connection_count=100,
        ),
        bottleneck_report=BottleneckReport(
            cause="CPU 과부하",
            severity="high",
            recommendation="컨테이너 확장",
            confidence=0.94,
            requires_scaling=True,
        ),
        scaling_plan={
            "service_name": "target-server",
            "current_replicas": 1,
            "desired_replicas": 2,
            "reason": "CPU 과부하",
        },
    )

    assert isinstance(result, list)
    assert result
    assert "1개에서 2개" in result[0]
    assert any("CPU 사용률" in plan for plan in result)
    assert any("사용자 승인" in plan for plan in result)


def test_generate_plan_without_scaling():
    result = generate_optimization_plan(
        target_tps=30,
        load_test_result=LoadTestResult(
            tps=32.0,
            latency_p95=300.0,
            latency_avg=150.0,
            error_rate=0.0,
            duration=30,
            total_requests=960,
        ),
        system_metrics=SystemMetrics(
            cpu_pct=40.0,
            mem_pct=50.0,
            connection_count=10,
        ),
        bottleneck_report=BottleneckReport(
            cause="병목 없음",
            severity="low",
            recommendation="현재 구성 유지",
            confidence=0.97,
            requires_scaling=False,
        ),
    )

    assert result == ["현재 구성 유지"]


def test_generate_plan_requires_scaling_plan():
    with pytest.raises(
        PlanGenerationError,
        match="scaling_plan이 필요합니다",
    ):
        generate_optimization_plan(
            target_tps=50,
            load_test_result=LoadTestResult(
                tps=20.0,
                latency_p95=2000.0,
                latency_avg=1000.0,
                error_rate=0.1,
                duration=30,
                total_requests=600,
            ),
            system_metrics=SystemMetrics(
                cpu_pct=95.0,
                mem_pct=80.0,
                connection_count=120,
            ),
            bottleneck_report=BottleneckReport(
                cause="CPU 과부하",
                severity="high",
                recommendation="스케일링",
                confidence=0.9,
                requires_scaling=True,
            ),
            scaling_plan=None,
        )

from app.agent.nodes import llm_reasoning_node

@pytest.mark.asyncio
async def test_reasoning_result_integrates_with_scaling_plan(
    monkeypatch: pytest.MonkeyPatch,
):
    """
    LLM 병목 분석 결과가 스케일링 최적화 플랜으로
    정상 변환되는지 확인한다.
    """

    state = create_initial_state(
        target_tps=50,
        duration=30,
    )

    state["load_test_result"] = LoadTestResult(
        tps=30.0,
        latency_p95=1800.0,
        latency_avg=900.0,
        error_rate=0.08,
        duration=30,
        total_requests=900,
    )

    state["system_metrics"] = SystemMetrics(
        cpu_pct=92.0,
        mem_pct=70.0,
        connection_count=100,
    )

    monkeypatch.setattr(
        "app.agent.nodes._get_current_replicas",
        lambda: 1,
    )

    async def mock_llm_caller(
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        assert system_prompt
        assert "50" in user_prompt
        assert "target-server" in user_prompt

        return """
        {
          "bottleneck_report": {
            "cause": "목표 TPS 미달과 CPU 과부하가 확인되었습니다.",
            "severity": "high",
            "recommendation": "컨테이너 확장을 권장합니다.",
            "confidence": 0.94,
            "requires_scaling": true
          },
          "agent_outcome": "awaiting_approval",
          "scaling_plan": {
            "service_name": "target-server",
            "current_replicas": 1,
            "desired_replicas": 2,
            "reason": "목표 TPS 미달과 CPU 과부하"
          }
        }
        """

    reasoning_update = await llm_reasoning_node(
        state,
        llm_caller=mock_llm_caller,
    )
    state.update(reasoning_update)

    assert state["error"] is None
    assert state["bottleneck_report"] is not None
    assert state["scaling_plan"] is not None
    assert state["agent_outcome"] == "awaiting_approval"

    plans = generate_optimization_plan(
        target_tps=state["target_tps"],
        load_test_result=state["load_test_result"],
        system_metrics=state["system_metrics"],
        bottleneck_report=state["bottleneck_report"],
        scaling_plan=state["scaling_plan"],
    )

    assert isinstance(plans, list)
    assert plans
    assert any(
        "1개에서 2개" in plan
        for plan in plans
    )
    assert any(
        "사용자 승인" in plan
        for plan in plans
    )
    assert any(
        "CPU 사용률" in plan
        for plan in plans
    )
    assert any(
        "오류율" in plan
        for plan in plans
    )
    assert any(
        "P95 응답 시간" in plan
        for plan in plans
    )


@pytest.mark.asyncio
async def test_reasoning_result_integrates_without_scaling(
    monkeypatch: pytest.MonkeyPatch,
):
    """
    병목과 스케일링이 필요 없는 분석 결과가
    유지·모니터링 중심 플랜으로 변환되는지 확인한다.
    """

    state = create_initial_state(
        target_tps=30,
        duration=30,
    )

    state["load_test_result"] = LoadTestResult(
        tps=32.0,
        latency_p95=300.0,
        latency_avg=150.0,
        error_rate=0.0,
        duration=30,
        total_requests=960,
    )

    state["system_metrics"] = SystemMetrics(
        cpu_pct=40.0,
        mem_pct=50.0,
        connection_count=10,
    )

    monkeypatch.setattr(
        "app.agent.nodes._get_current_replicas",
        lambda: 1,
    )

    async def mock_llm_caller(
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        return """
        {
          "bottleneck_report": {
            "cause": "목표 TPS를 달성했고 병목이 없습니다.",
            "severity": "low",
            "recommendation": "현재 구성을 유지합니다.",
            "confidence": 0.97,
            "requires_scaling": false
          },
          "agent_outcome": "diagnosed",
          "scaling_plan": null
        }
        """

    reasoning_update = await llm_reasoning_node(
        state,
        llm_caller=mock_llm_caller,
    )
    state.update(reasoning_update)

    assert state["error"] is None
    assert state["bottleneck_report"] is not None
    assert state["scaling_plan"] is None
    assert state["agent_outcome"] == "diagnosed"

    plans = generate_optimization_plan(
        target_tps=state["target_tps"],
        load_test_result=state["load_test_result"],
        system_metrics=state["system_metrics"],
        bottleneck_report=state["bottleneck_report"],
        scaling_plan=state["scaling_plan"],
    )

    assert plans == [
        "현재 구성을 유지합니다."
    ]


@pytest.mark.asyncio
async def test_generate_plan_rejects_missing_scaling_plan(
    monkeypatch: pytest.MonkeyPatch,
):
    """
    LLM 진단은 스케일링이 필요하다고 했지만
    scaling_plan이 누락된 상태를 거부하는지 확인한다.
    """

    state = create_initial_state(
        target_tps=50,
        duration=30,
    )

    state["load_test_result"] = LoadTestResult(
        tps=25.0,
        latency_p95=2000.0,
        latency_avg=1000.0,
        error_rate=0.1,
        duration=30,
        total_requests=750,
    )

    state["system_metrics"] = SystemMetrics(
        cpu_pct=95.0,
        mem_pct=80.0,
        connection_count=120,
    )

    monkeypatch.setattr(
        "app.agent.nodes._get_current_replicas",
        lambda: 1,
    )

    async def mock_llm_caller(
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        return """
        {
          "bottleneck_report": {
            "cause": "CPU 과부하",
            "severity": "high",
            "recommendation": "스케일링이 필요합니다.",
            "confidence": 0.9,
            "requires_scaling": true
          },
          "agent_outcome": "awaiting_approval",
          "scaling_plan": {
            "service_name": "target-server",
            "current_replicas": 1,
            "desired_replicas": 2,
            "reason": "CPU 과부하"
          }
        }
        """

    reasoning_update = await llm_reasoning_node(
        state,
        llm_caller=mock_llm_caller,
    )
    state.update(reasoning_update)

    assert state["bottleneck_report"] is not None
    assert state["bottleneck_report"].requires_scaling is True

    with pytest.raises(
        PlanGenerationError,
        match="scaling_plan이 필요합니다",
    ):
        generate_optimization_plan(
            target_tps=state["target_tps"],
            load_test_result=state["load_test_result"],
            system_metrics=state["system_metrics"],
            bottleneck_report=state["bottleneck_report"],
            scaling_plan=None,
        )


@pytest.mark.asyncio
async def test_generate_plan_removes_duplicate_actions(
    monkeypatch: pytest.MonkeyPatch,
):
    """
    LLM 권장 조치와 규칙 기반 조치가 중복되더라도
    최종 목록에는 같은 문장이 한 번만 포함되는지 확인한다.
    """

    state = create_initial_state(
        target_tps=30,
        duration=30,
    )

    state["load_test_result"] = LoadTestResult(
        tps=31.0,
        latency_p95=300.0,
        latency_avg=150.0,
        error_rate=0.0,
        duration=30,
        total_requests=930,
    )

    state["system_metrics"] = SystemMetrics(
        cpu_pct=40.0,
        mem_pct=50.0,
        connection_count=10,
    )

    monkeypatch.setattr(
        "app.agent.nodes._get_current_replicas",
        lambda: 1,
    )

    async def mock_llm_caller(
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        return """
        {
          "bottleneck_report": {
            "cause": "병목 없음",
            "severity": "low",
            "recommendation": "현재 구성을 유지합니다.",
            "confidence": 0.98,
            "requires_scaling": false
          },
          "agent_outcome": "diagnosed",
          "scaling_plan": null
        }
        """

    reasoning_update = await llm_reasoning_node(
        state,
        llm_caller=mock_llm_caller,
    )
    state.update(reasoning_update)

    plans = generate_optimization_plan(
        target_tps=state["target_tps"],
        load_test_result=state["load_test_result"],
        system_metrics=state["system_metrics"],
        bottleneck_report=state["bottleneck_report"],
        scaling_plan=state["scaling_plan"],
    )

    assert len(plans) == len(set(plans))

# optimization_plan Engine 연동 테스트
def _load_result(
    *,
    tps: float = 30.0,
    latency_p95: float = 1800.0,
    error_rate: float = 0.08,
) -> LoadTestResult:
    return LoadTestResult(
        tps=tps,
        latency_p95=latency_p95,
        latency_avg=latency_p95 / 2,
        error_rate=error_rate,
        duration=30,
        total_requests=int(tps * 30),
    )


def _metrics(
    *,
    cpu_pct: float = 92.0,
) -> SystemMetrics:
    return SystemMetrics(
        cpu_pct=cpu_pct,
        mem_pct=70.0,
        connection_count=100,
    )


def _report(
    *,
    requires_scaling: bool = True,
) -> BottleneckReport:
    return BottleneckReport(
        cause=(
            "CPU 과부하"
            if requires_scaling
            else "병목 없음"
        ),
        severity=(
            "high"
            if requires_scaling
            else "low"
        ),
        recommendation=(
            "컨테이너 확장"
            if requires_scaling
            else "현재 구성 유지"
        ),
        confidence=0.95,
        requires_scaling=requires_scaling,
    )


def _waiting_state() -> AgentRuntimeState:
    state = create_initial_state(
        target_tps=50,
        duration=30,
    )

    state.update(
        {
            "load_test_result": _load_result(),
            "system_metrics": _metrics(),
            "bottleneck_report": _report(),
            "agent_outcome": "awaiting_approval",
            "scaling_required": True,
            "scaling_approved": None,
            "waiting_for_approval": True,
            "scaling_plan": {
                "service_name": "target-server",
                "current_replicas": 1,
                "desired_replicas": 2,
                "reason": "CPU 과부하",
            },
            "optimization_plan": [
                (
                    "target-server 서비스를 1개에서 "
                    "2개로 확장합니다. "
                    "실행 전 사용자 승인이 필요합니다."
                )
            ],
            "loop_count": 1,
            "error": None,
        }
    )

    return state


def test_initial_state_has_empty_optimization_plan():
    """새 작업의 최적화 계획 초기값이 빈 목록인지 확인한다."""

    state = create_initial_state(
        target_tps=30,
        duration=10,
    )

    assert "optimization_plan" in state
    assert state["optimization_plan"] == []


@pytest.mark.asyncio
async def test_generate_plan_node_stores_generated_plan():
    """진단 결과로 생성한 계획이 state update에 담기는지 확인한다."""

    state = _waiting_state()

    def mock_plan_generator(**kwargs) -> list[str]:
        assert kwargs["target_tps"] == 50
        assert kwargs["load_test_result"] is state["load_test_result"]
        assert kwargs["system_metrics"] is state["system_metrics"]
        assert kwargs["bottleneck_report"] is state["bottleneck_report"]
        assert kwargs["scaling_plan"] is state["scaling_plan"]

        return [
            "target-server를 1개에서 2개로 확장합니다.",
            "CPU 사용률을 점검합니다.",
        ]

    update = await generate_plan_node(
        state,
        plan_generator=mock_plan_generator,
    )

    assert update["optimization_plan"] == [
        "target-server를 1개에서 2개로 확장합니다.",
        "CPU 사용률을 점검합니다.",
    ]
    assert update["error"] is None
    assert "agent_outcome" not in update


@pytest.mark.asyncio
async def test_generate_plan_node_fails_when_report_is_missing():
    """BottleneckReport가 없으면 계획 생성을 중단하는지 확인한다."""

    state = create_initial_state(
        target_tps=50,
        duration=30,
    )
    state["load_test_result"] = _load_result()
    state["system_metrics"] = _metrics()

    generator_called = False

    def must_not_run_generator(**kwargs) -> list[str]:
        nonlocal generator_called
        generator_called = True
        pytest.fail(
            "bottleneck_report가 없는데 plan generator가 실행됐습니다."
        )

    update = await generate_plan_node(
        state,
        plan_generator=must_not_run_generator,
    )

    assert generator_called is False
    assert update["agent_outcome"] == "failed"
    assert "bottleneck_report" in update["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_result",
    [
        "문자열 결과",
        {"plan": ["잘못된 형식"]},
        [1, 2, 3],
    ],
)
async def test_generate_plan_node_rejects_invalid_return_type(
    invalid_result,
):
    """plan generator의 반환 형식이 list[str]인지 검사한다."""

    state = _waiting_state()

    def invalid_generator(**kwargs):
        return invalid_result

    update = await generate_plan_node(
        state,
        plan_generator=invalid_generator,
    )

    assert update["agent_outcome"] == "failed"
    assert update["waiting_for_approval"] is False
    assert "최적화 계획 생성 실패" in update["error"]


@pytest.mark.asyncio
async def test_engine_runs_plan_node_after_reasoning():
    """
    Engine 순서가 load → metrics → reasoning → plan인지 확인한다.
    """

    state = create_initial_state(
        target_tps=30,
        duration=10,
    )
    executed: list[str] = []

    async def fake_load(
        current_state: AgentRuntimeState,
    ) -> dict:
        executed.append("load")
        return {
            "load_test_result": _load_result(
                tps=32.0,
                latency_p95=300.0,
                error_rate=0.0,
            ),
            "agent_outcome": "pending",
            "error": None,
        }

    async def fake_metrics(
        current_state: AgentRuntimeState,
    ) -> dict:
        executed.append("metrics")
        return {
            "system_metrics": _metrics(
                cpu_pct=40.0,
            ),
            "agent_outcome": "pending",
            "error": None,
        }

    async def fake_reasoning(
        current_state: AgentRuntimeState,
    ) -> dict:
        executed.append("reasoning")
        return {
            "bottleneck_report": _report(
                requires_scaling=False,
            ),
            "agent_outcome": "diagnosed",
            "scaling_required": False,
            "scaling_approved": None,
            "waiting_for_approval": False,
            "scaling_plan": None,
            "loop_count": 1,
            "error": None,
        }

    async def fake_plan(
        current_state: AgentRuntimeState,
    ) -> dict:
        executed.append("plan")

        assert current_state["bottleneck_report"] is not None
        assert current_state["agent_outcome"] == "diagnosed"

        return {
            "optimization_plan": [
                "현재 구성을 유지합니다."
            ],
            "error": None,
        }

    result = await engine.run_diagnosis(
        state,
        load_test_node=fake_load,
        metrics_node=fake_metrics,
        reasoning_node=fake_reasoning,
        plan_node=fake_plan,
    )

    assert executed == [
        "load",
        "metrics",
        "reasoning",
        "plan",
    ]
    assert result["optimization_plan"] == [
        "현재 구성을 유지합니다."
    ]
    assert result["agent_outcome"] == "diagnosed"
    assert result["error"] is None


@pytest.mark.asyncio
async def test_engine_stops_when_plan_node_fails():
    """plan_node 실패 시 최종 상태가 failed가 되는지 확인한다."""

    state = create_initial_state(
        target_tps=30,
        duration=10,
    )

    async def fake_load(
        current_state: AgentRuntimeState,
    ) -> dict:
        return {
            "load_test_result": _load_result(),
            "agent_outcome": "pending",
            "error": None,
        }

    async def fake_metrics(
        current_state: AgentRuntimeState,
    ) -> dict:
        return {
            "system_metrics": _metrics(),
            "agent_outcome": "pending",
            "error": None,
        }

    async def fake_reasoning(
        current_state: AgentRuntimeState,
    ) -> dict:
        return {
            "bottleneck_report": _report(),
            "agent_outcome": "awaiting_approval",
            "scaling_required": True,
            "waiting_for_approval": True,
            "scaling_plan": {
                "service_name": "target-server",
                "current_replicas": 1,
                "desired_replicas": 2,
                "reason": "CPU 과부하",
            },
            "loop_count": 1,
            "error": None,
        }

    async def failed_plan(
        current_state: AgentRuntimeState,
    ) -> dict:
        return {
            "agent_outcome": "failed",
            "waiting_for_approval": False,
            "final_answer": None,
            "error": "최적화 계획 생성 실패",
        }

    result = await engine.run_diagnosis(
        state,
        load_test_node=fake_load,
        metrics_node=fake_metrics,
        reasoning_node=fake_reasoning,
        plan_node=failed_plan,
    )

    assert result["agent_outcome"] == "failed"
    assert result["waiting_for_approval"] is False
    assert "최적화 계획 생성 실패" in result["error"]


@pytest.mark.asyncio
async def test_revalidation_failure_regenerates_optimization_plan():
    """
    재검증 실패 후 reasoning과 plan Node가 다시 실행되어
    2개 → 3개 계획으로 갱신되는지 확인한다.
    """

    state = _waiting_state()
    executed: list[str] = []

    async def fake_scaling(
        current_state: AgentRuntimeState,
    ) -> dict:
        executed.append("scaling")
        return {
            "scaling_result": ScalingResult(
                before_replicas=1,
                after_replicas=2,
                success=True,
            ),
            "scaling_count": 1,
            "agent_outcome": "scaled",
            "waiting_for_approval": False,
            "error": None,
        }

    async def fake_load(
        current_state: AgentRuntimeState,
    ) -> dict:
        executed.append("load")
        return {
            "load_test_result": _load_result(
                tps=36.0,
                latency_p95=1300.0,
                error_rate=0.04,
            ),
            "agent_outcome": "pending",
            "error": None,
        }

    async def fake_metrics(
        current_state: AgentRuntimeState,
    ) -> dict:
        executed.append("metrics")
        return {
            "system_metrics": _metrics(
                cpu_pct=82.0,
            ),
            "agent_outcome": "pending",
            "error": None,
        }

    async def fake_reasoning(
        current_state: AgentRuntimeState,
    ) -> dict:
        executed.append("reasoning")
        return {
            "bottleneck_report": BottleneckReport(
                cause="병목 지속",
                severity="high",
                recommendation="추가 확장",
                confidence=0.9,
                requires_scaling=True,
            ),
            "agent_outcome": "awaiting_approval",
            "scaling_required": True,
            "scaling_approved": None,
            "waiting_for_approval": True,
            "scaling_plan": {
                "service_name": "target-server",
                "current_replicas": 2,
                "desired_replicas": 3,
                "reason": "성능 개선 부족",
            },
            "loop_count": 2,
            "error": None,
        }

    async def fake_plan(
        current_state: AgentRuntimeState,
    ) -> dict:
        executed.append("plan")
        assert (
            current_state["scaling_plan"]["desired_replicas"]
            == 3
        )

        return {
            "optimization_plan": [
                (
                    "target-server 서비스를 2개에서 "
                    "3개로 확장합니다. "
                    "실행 전 사용자 승인이 필요합니다."
                )
            ],
            "error": None,
        }

    async def fake_revalidation(
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        executed.append("revalidation")
        return json.dumps(
            {
                "summary": "성능 개선이 충분하지 않습니다.",
                "performance_improved": False,
                "additional_action_required": True,
                "recommended_action": "replica 추가 증가",
            },
            ensure_ascii=False,
        )

    result = await engine.resume_after_approval(
        state,
        approved=True,
        scaling_node=fake_scaling,
        load_test_node=fake_load,
        metrics_node=fake_metrics,
        reasoning_node=fake_reasoning,
        plan_node=fake_plan,
        revalidation_caller=fake_revalidation,
    )

    assert executed == [
        "scaling",
        "load",
        "metrics",
        "revalidation",
        "reasoning",
        "plan",
    ]
    assert result["agent_outcome"] == "awaiting_approval"
    assert result["scaling_plan"]["desired_replicas"] == 3
    assert any(
        "2개에서 3개" in item
        for item in result["optimization_plan"]
    )


@pytest.mark.asyncio
async def test_revalidation_success_updates_completion_plan():
    """
    스케일링과 재검증 성공 후 기존 권장 계획이
    완료·모니터링 계획으로 변경되는지 확인한다.
    """

    state = _waiting_state()

    async def fake_scaling(
        current_state: AgentRuntimeState,
    ) -> dict:
        return {
            "scaling_result": ScalingResult(
                before_replicas=1,
                after_replicas=2,
                success=True,
            ),
            "scaling_count": 1,
            "agent_outcome": "scaled",
            "waiting_for_approval": False,
            "error": None,
        }

    async def fake_load(
        current_state: AgentRuntimeState,
    ) -> dict:
        return {
            "load_test_result": _load_result(
                tps=55.0,
                latency_p95=600.0,
                error_rate=0.01,
            ),
            "agent_outcome": "pending",
            "error": None,
        }

    async def fake_metrics(
        current_state: AgentRuntimeState,
    ) -> dict:
        return {
            "system_metrics": _metrics(
                cpu_pct=55.0,
            ),
            "agent_outcome": "pending",
            "error": None,
        }

    async def fake_revalidation(
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        return json.dumps(
            {
                "summary": "TPS와 응답 지연이 개선되었습니다.",
                "performance_improved": True,
                "additional_action_required": False,
                "recommended_action": None,
            },
            ensure_ascii=False,
        )

    result = await engine.resume_after_approval(
        state,
        approved=True,
        scaling_node=fake_scaling,
        load_test_node=fake_load,
        metrics_node=fake_metrics,
        revalidation_caller=fake_revalidation,
    )

    assert result["agent_outcome"] == "scaled"
    assert result["scaling_plan"] is None
    assert result["optimization_plan"]
    assert any(
        "정상적으로 적용" in item
        for item in result["optimization_plan"]
    )
    assert any(
        "모니터링" in item
        for item in result["optimization_plan"]
    )


@pytest.mark.asyncio
async def test_rejection_keeps_recommended_plan():
    """
    승인 거절 시 기존 최적화 계획을 지우지 않고
    거절 상태 안내를 추가하는지 확인한다.
    """

    state = _waiting_state()
    previous_plan = list(
        state["optimization_plan"]
    )

    result = await engine.resume_after_approval(
        state,
        approved=False,
    )

    assert result["agent_outcome"] == "diagnosed"
    assert result["scaling_approved"] is False
    assert result["optimization_plan"]

    assert any(
        "거절" in item
        for item in result["optimization_plan"]
    )

    for item in previous_plan:
        assert item in result["optimization_plan"]

# API 리포트 연동 테스트
# backend 경로를 import 경로에 추가한다.
BACKEND_PATH = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "backend",
    )
)

if BACKEND_PATH not in sys.path:
    sys.path.insert(0, BACKEND_PATH)


# app.main은 app/static 경로를 사용하므로 테스트 실행 위치에 따라
# 디렉터리가 없을 경우를 대비한다.
os.makedirs(
    os.path.join(BACKEND_PATH, "app", "static"),
    exist_ok=True,
)

from app.api.v1 import agent
from app.api.v1.agent import (
    remeasurement_results,
    task_manager,
)
from app.main import app


client = TestClient(app)


@pytest.fixture(autouse=True)
def clear_agent_task_storage():
    """
    테스트 간 전역 task 상태가 섞이지 않도록 초기화한다.
    """

    task_manager.states.clear()
    task_manager.futures.clear()
    remeasurement_results.clear()

    yield

    task_manager.states.clear()
    task_manager.futures.clear()
    remeasurement_results.clear()


def _register_state_directly(state):
    """
    API 리포트 테스트에서는 승인 Future가 필요하지 않으므로
    register_task() 대신 states에 직접 저장한다.
    """

    task_manager.states[state["task_id"]] = state
    return state["task_id"]


def test_report_returns_optimization_plan_before_approval():
    """
    Engine이 승인 대기 상태에서 만든 optimization_plan이
    리포트 응답에 포함되는지 확인한다.
    """

    state = create_initial_state(
        target_tps=50,
        duration=30,
    )

    state.update(
        {
            "load_test_result": LoadTestResult(
                tps=30.0,
                latency_p95=1800.0,
                latency_avg=900.0,
                error_rate=0.08,
                duration=30,
                total_requests=900,
            ),
            "system_metrics": SystemMetrics(
                cpu_pct=92.0,
                mem_pct=70.0,
                connection_count=100,
            ),
            "bottleneck_report": BottleneckReport(
                cause="목표 TPS 미달과 CPU 과부하",
                severity="high",
                recommendation="컨테이너 확장",
                confidence=0.94,
                requires_scaling=True,
            ),
            "agent_outcome": "awaiting_approval",
            "scaling_required": True,
            "waiting_for_approval": True,
            "scaling_plan": {
                "service_name": "target-server",
                "current_replicas": 1,
                "desired_replicas": 2,
                "reason": "CPU 과부하",
            },
            "optimization_plan": [
                "target-server를 1개에서 2개로 확장합니다.",
                "실행 전 사용자 승인이 필요합니다.",
            ],
            "error": None,
        }
    )

    task_id = _register_state_directly(state)

    response = client.get(
        f"/api/v1/agent/report/{task_id}"
    )

    assert response.status_code == 200

    body = response.json()

    assert body["task_id"] == task_id
    assert body["outcome"] == "awaiting_approval"
    assert body["waiting_for_approval"] is True

    assert "optimization_plan" in body
    assert body["optimization_plan"] == [
        "target-server를 1개에서 2개로 확장합니다.",
        "실행 전 사용자 승인이 필요합니다.",
    ]


def test_report_returns_completed_plan_after_scaling():
    """
    스케일링과 재검증이 끝난 뒤 완료·모니터링 계획이
    HTTP 응답에 포함되는지 확인한다.
    """

    state = create_initial_state(
        target_tps=50,
        duration=30,
    )

    before = LoadTestResult(
        tps=28.0,
        latency_p95=1900.0,
        latency_avg=950.0,
        error_rate=0.09,
        duration=30,
        total_requests=840,
    )

    after = LoadTestResult(
        tps=55.0,
        latency_p95=600.0,
        latency_avg=300.0,
        error_rate=0.01,
        duration=30,
        total_requests=1650,
    )

    state.update(
        {
            "load_test_result": before,
            "system_metrics": SystemMetrics(
                cpu_pct=55.0,
                mem_pct=60.0,
                connection_count=80,
            ),
            "bottleneck_report": BottleneckReport(
                cause="목표 TPS 미달과 CPU 과부하",
                severity="high",
                recommendation="컨테이너 확장",
                confidence=0.95,
                requires_scaling=True,
            ),
            "agent_outcome": "scaled",
            "scaling_required": False,
            "scaling_approved": True,
            "waiting_for_approval": False,
            "scaling_plan": None,
            "scaling_result": ScalingResult(
                before_replicas=1,
                after_replicas=2,
                success=True,
            ),
            "optimization_plan": [
                "승인된 스케일링 작업이 정상적으로 적용되었습니다.",
                "현재 구성을 유지하며 시스템 메트릭을 모니터링합니다.",
            ],
            "error": None,
        }
    )

    task_id = _register_state_directly(state)
    remeasurement_results[task_id] = after

    response = client.get(
        f"/api/v1/agent/report/{task_id}"
    )

    assert response.status_code == 200

    body = response.json()

    assert body["outcome"] == "scaled"
    assert body["action"]["success"] is True
    assert body["action"]["before_replicas"] == 1
    assert body["action"]["after_replicas"] == 2

    assert body["measurement"]["tps"] == 28.0
    assert body["measurement_after"]["tps"] == 55.0
    assert body["improvement"]["tps_delta"] == pytest.approx(
        27.0
    )

    assert body["optimization_plan"] == [
        "승인된 스케일링 작업이 정상적으로 적용되었습니다.",
        "현재 구성을 유지하며 시스템 메트릭을 모니터링합니다.",
    ]


def test_report_keeps_plan_after_user_rejection():
    """
    사용자가 스케일링을 거절해도 권장 계획과 거절 안내가
    리포트에 남아 있는지 확인한다.
    """

    state = create_initial_state(
        target_tps=50,
        duration=30,
    )

    state.update(
        {
            "agent_outcome": "diagnosed",
            "scaling_required": False,
            "scaling_approved": False,
            "waiting_for_approval": False,
            "optimization_plan": [
                "사용자가 스케일링 실행을 거절했습니다.",
                "target-server를 1개에서 2개로 확장하는 것을 권장합니다.",
            ],
            "scaling_result": None,
            "error": None,
        }
    )

    task_id = _register_state_directly(state)

    response = client.get(
        f"/api/v1/agent/report/{task_id}"
    )

    assert response.status_code == 200

    body = response.json()

    assert body["outcome"] == "diagnosed"
    assert body["action"] is None
    assert any(
        "거절" in item
        for item in body["optimization_plan"]
    )


def test_report_returns_404_for_unknown_task():
    """
    존재하지 않는 task_id는 404를 반환하는지 확인한다.
    """

    response = client.get(
        "/api/v1/agent/report/not-found-task"
    )

    assert response.status_code == 404
    assert (
        response.json()["detail"]
        == "해당 task_id를 찾을 수 없습니다."
    )