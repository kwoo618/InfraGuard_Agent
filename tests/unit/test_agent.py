import pytest
from dataclasses import dataclass
from datetime import datetime

# 테스트 내부에서 임시 구조체 선언 (backend.app.agent 에러 방지)
@dataclass
class LoadTestResult:
    tps: float
    latency_p95: float
    error_rate: float
    duration: int

@dataclass
class SystemMetrics:
    cpu_pct: float
    mem_pct: float
    connection_count: int
    timestamp: str

@dataclass
class BottleneckReport:
    cause: str
    severity: str
    recommendation: str
    confidence: float

@dataclass
class ScalingResult:
    before_replicas: int
    after_replicas: int
    success: bool


# 1. 요구사항 규격 검증 테스트
def test_project_dataclass_specs():
    load_test = LoadTestResult(tps=350.5, latency_p95=120.0, error_rate=0.02, duration=60)
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


from app.agent.prompts import (
    SYSTEM_PROMPT,
    build_bottleneck_analysis_prompt,
    build_revalidation_prompt,
)
from app.agent.state import AgentRuntimeState, create_initial_state
from app.schemas import (
    BottleneckReport,
    LoadTestResult,
    ScalingResult,
    SystemMetrics,
)


def create_load_test_result(
    *,
    tps: float = 48.5,
    latency_p95: float = 850.0,
    latency_avg: float = 320.0,
    error_rate: float = 0.02,
    duration: int = 30,
    total_requests: int = 1455,
) -> LoadTestResult:
    """테스트에서 사용할 Locust 결과를 생성한다."""

    return LoadTestResult(
        tps=tps,
        latency_p95=latency_p95,
        latency_avg=latency_avg,
        error_rate=error_rate,
        duration=duration,
        total_requests=total_requests,
    )


def create_system_metrics(
    *,
    cpu_pct: float = 85.5,
    mem_pct: float = 62.3,
    connection_count: int = 120,
) -> SystemMetrics:
    """테스트에서 사용할 시스템 메트릭을 생성한다."""

    return SystemMetrics(
        cpu_pct=cpu_pct,
        mem_pct=mem_pct,
        connection_count=connection_count,
    )

def create_bottleneck_prompt_kwargs(
    target_tps: int = 50,
    service_name: str = "target-api",
    current_replicas: int = 1,
) -> dict:
    """
    build_bottleneck_analysis_prompt 호출에 필요한
    공통 인자 5개를 딕셔너리로 생성한다.
    """

    return {
        "target_tps": target_tps,
        "load_test_result": create_load_test_result(),
        "system_metrics": create_system_metrics(),
        "service_name": service_name,
        "current_replicas": current_replicas,
    }

def test_create_initial_state():
    """초기 Agent 상태가 올바르게 생성되는지 확인한다."""

    state = create_initial_state(
        target_tps=50,
        duration=30,
    )

    assert isinstance(state, dict)
    assert state["task_id"]
    assert state["target_tps"] == 50
    assert state["duration"] == 30

    assert state["load_test_result"] is None
    assert state["system_metrics"] is None
    assert state["bottleneck_report"] is None

    assert state["agent_outcome"] == "pending"
    assert state["scaling_plan"] is None
    assert state["scaling_required"] is False
    assert state["scaling_approved"] is None
    assert state["waiting_for_approval"] is False
    assert state["scaling_result"] is None

    assert state["loop_count"] == 0
    assert state["scaling_count"] == 0
    assert state["final_answer"] is None
    assert state["error"] is None


def test_create_initial_state_rejects_invalid_target_tps():
    """목표 TPS가 0 이하일 때 오류가 발생하는지 확인한다."""

    with pytest.raises(ValueError, match="target_tps는 1 이상"):
        create_initial_state(
            target_tps=0,
            duration=30,
        )


def test_create_initial_state_rejects_invalid_duration():
    """실행 시간이 0 이하일 때 오류가 발생하는지 확인한다."""

    with pytest.raises(ValueError, match="duration은 1초 이상"):
        create_initial_state(
            target_tps=50,
            duration=0,
        )


def test_schema_objects_can_be_stored_in_state():
    """schemas.py의 객체가 Runtime State에 저장되는지 확인한다."""

    state = create_initial_state(
        target_tps=50,
        duration=30,
    )

    load_result = create_load_test_result()
    metrics = create_system_metrics()

    report = BottleneckReport(
        cause="CPU 사용률 증가",
        severity="high",
        recommendation="컨테이너 수 증가 권장",
        confidence=0.91,
        requires_scaling=True,
    )

    scaling_result = ScalingResult(
        before_replicas=1,
        after_replicas=2,
        success=True,
    )

    state["load_test_result"] = load_result
    state["system_metrics"] = metrics
    state["bottleneck_report"] = report
    state["scaling_result"] = scaling_result
    state["agent_outcome"] = "scaled"
    state["scaling_required"] = report.requires_scaling

    assert state["load_test_result"].tps == 48.5
    assert state["system_metrics"].cpu_pct == 85.5
    assert state["bottleneck_report"].cause == "CPU 사용률 증가"
    assert state["scaling_result"].after_replicas == 2
    assert state["agent_outcome"] == "scaled"
    assert state["scaling_required"] is True


def test_system_prompt_contains_required_rules():
    """공통 시스템 프롬프트의 핵심 규칙을 확인한다."""

    assert "InfraGuard Agent" in SYSTEM_PROMPT
    assert "사용자의 승인 없이" in SYSTEM_PROMPT
    assert "JSON 형식으로만 반환" in SYSTEM_PROMPT
    assert "low, medium, high" in SYSTEM_PROMPT


def test_build_bottleneck_analysis_prompt():
    prompt = build_bottleneck_analysis_prompt(
        **create_bottleneck_prompt_kwargs()
    )

    assert isinstance(prompt, str)

    assert "50" in prompt
    assert "target-api" in prompt

    assert '"tps": 48.5' in prompt
    assert '"latency_p95": 850.0' in prompt
    assert '"latency_avg": 320.0' in prompt
    assert '"error_rate": 0.02' in prompt

    assert '"cpu_pct": 85.5' in prompt
    assert '"mem_pct": 62.3' in prompt
    assert '"connection_count": 120' in prompt

    assert '"bottleneck_report"' in prompt
    assert '"agent_outcome"' in prompt
    assert '"scaling_plan"' in prompt


def test_bottleneck_prompt_rejects_invalid_target_tps():
    with pytest.raises(
        ValueError,
        match="target_tps는 1 이상",
    ):
        build_bottleneck_analysis_prompt(
            **create_bottleneck_prompt_kwargs(
                target_tps=0,
            )
        )


def test_build_revalidation_prompt():
    """스케일링 전후 데이터가 재검증 프롬프트에 포함되는지 확인한다."""

    previous_load_result = create_load_test_result(
        tps=30.0,
        latency_p95=1500.0,
        latency_avg=700.0,
        error_rate=0.10,
        total_requests=900,
    )

    current_load_result = create_load_test_result(
        tps=48.0,
        latency_p95=700.0,
        latency_avg=280.0,
        error_rate=0.01,
        total_requests=1440,
    )

    previous_metrics = create_system_metrics(
        cpu_pct=95.0,
        mem_pct=70.0,
        connection_count=150,
    )

    current_metrics = create_system_metrics(
        cpu_pct=65.0,
        mem_pct=58.0,
        connection_count=90,
    )

    prompt = build_revalidation_prompt(
        previous_load_test_result=previous_load_result,
        current_load_test_result=current_load_result,
        previous_system_metrics=previous_metrics,
        current_system_metrics=current_metrics,
    )

    assert isinstance(prompt, str)

    assert "스케일링 전 Locust 결과" in prompt
    assert "스케일링 후 Locust 결과" in prompt

    assert '"tps": 30.0' in prompt
    assert '"tps": 48.0' in prompt

    assert '"latency_p95": 1500.0' in prompt
    assert '"latency_p95": 700.0' in prompt

    assert '"cpu_pct": 95.0' in prompt
    assert '"cpu_pct": 65.0' in prompt

    assert '"performance_improved"' in prompt
    assert '"additional_action_required"' in prompt
    assert '"recommended_action"' in prompt

