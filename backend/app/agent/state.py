from typing import Any, TypedDict
from uuid import uuid4

class AgentState(TypedDict):

    # 각 진단 작업을 구분하기 위한 고유 식별자
    # 예: "7a09c994-2090-4d50-b01b-e508ef16731f"
    task_id: str


    # 사용자가 요청한 목표 TPS
    # Locust가 초당 몇 건의 요청을 발생시킬지 나타낸다.
    target_tps: int

    # 사용자가 요청한 부하 테스트 실행 시간
    # 단위는 초로 사용한다.
    duration: int

    # Locust 부하 테스트 실행 결과
    # run_load_test_node가 실행된 후 값이 저장된다.
    load_test_result: dict[str, Any] | None

    # Prometheus에서 수집한 시스템 리소스 메트릭
    # collect_metrics_node가 실행된 후 값이 저장된다.
    system_metrics: dict[str, Any] | None

    # Locust 결과와 Prometheus 메트릭을 바탕으로
    # LLM이 분석한 병목 진단 결과
    # llm_reasoning_node가 실행된 후 값이 저장된다.
    bottleneck_report: dict[str, Any] | None

    # LLM이 전체 분석 결과를 바탕으로 내린 최종 판단
    agent_outcome:dict[str, Any] | None

    # 병목을 해결하기 위해 LLM이 생성한 스케일링 계획
    scaling_plan: dict[str, Any] | None

    # 현재 분석 결과에서 스케일링이 필요한지 나타내는 값
    scaling_required: bool

    # 사용자가 스케일링 실행을 승인했는지 나타내는 값
    scaling_approved: bool | None

    # Agent가 현재 사용자 승인을 기다리고 있는지 나타내는 값
    waiting_for_approval: bool

    # 현재 ReAct Loop가 몇 번 실행되었는지 기록하는 값
    iteration: int

    # 현재 진단 작업에서 스케일링을 실행한 횟수
    scaling_count: int

    # 모든 진단과 조치가 완료된 후 사용자에게 전달할 최종 답변
    final_answer: str | None

    # Agent 또는 Tool 실행 중 발생한 오류 메시지
    error: str | None


def create_initial_state(
    target_tps: int,
    duration: int,
) -> AgentState:
    return AgentState(
        task_id=str(uuid4()),
        target_tps=target_tps,
        duration=duration,
        load_test_result=None,
        system_metrics=None,
        bottleneck_report=None,
        agent_outcome=None,
        scaling_plan=None,
        scaling_required=False,
        scaling_approved=None,
        waiting_for_approval=False,
        iteration=0,
        scaling_count=0,
        final_answer=None,
        error=None,
    )