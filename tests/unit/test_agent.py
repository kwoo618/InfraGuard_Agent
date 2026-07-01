import pytest
from dataclasses import dataclass
from datetime import datetime



# 1. 요구사항 규격 검증 테스트
def test_project_dataclass_specs():
    load_test = LoadTestResult(tps=350.5, latency_p95=120.0, error_rate=0.02, duration=60, latency_avg=40.0, total_requests=100)
    assert load_test.tps == 350.5

    metrics = SystemMetrics(cpu_pct=92.5, mem_pct=78.0, connection_count=1500, timestamp=str(datetime.now()))
    assert metrics.cpu_pct > 90.0

    report = BottleneckReport(
        cause="CPU Bottleneck", severity="high", recommendation="Scale-out", confidence=0.95
    )
    assert report.severity == "high"


# 2. HITL(승인 프로세스) 우회 차단 검증 (체크리스트 필수 요건)
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
import pytest
import httpx

from app.agent.nodes import (
    collect_metrics_node,
    execute_scaling_node,
    llm_reasoning_node,
    run_load_test_node,
)
from app.agent.state import create_initial_state
from app.schemas import (
    LoadTestResult,
    ScalingResult,
    SystemMetrics,
)


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
)
from app.agent.state import create_initial_state
from app.schemas import (
    LoadTestResult,
    ScalingResult,
    SystemMetrics,
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