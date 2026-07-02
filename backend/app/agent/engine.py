"""
InfraGuard Agent의 ReAct Loop 실행 흐름을 관리하는 오케스트레이터.

각 Node는 변경할 상태값만 반환하므로,
Engine은 Node 실행 결과를 기존 AgentRuntimeState에 병합한다.

실행 흐름:
    create_initial_state
        → run_load_test_node
        → collect_metrics_node
        → llm_reasoning_node
        → 스케일링 불필요: 종료
        → 스케일링 필요: 사용자 승인 대기
        → 승인 후 execute_scaling_node
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from app.agent.nodes import (
    MAX_LOOP,
    SYSTEM_PROMPT,
    call_solar_api,
    collect_metrics_node,
    execute_scaling_node,
    llm_reasoning_node,
    run_load_test_node,
)
from app.agent.prompts import build_revalidation_prompt

from app.agent.state import (
    AgentRuntimeState,
    create_initial_state,
)

from app.schemas import (
    LoadTestResult,
    SystemMetrics,
)

ENGINE_TIMEOUT_SECONDS = 300

# Node 함수가 공통으로 따르는 타입
AgentNode = Callable[
    [AgentRuntimeState],
    Awaitable[dict[str, Any]],
]

RevalidationCaller = Callable[
    [str, str],
    Awaitable[str],
]

class AgentEngineError(RuntimeError):
    """Agent Engine 실행 과정에서 발생하는 오류."""

def _ensure_loop_available(
    state: AgentRuntimeState,
) -> None:
    """
    다음 LLM 분석 Loop를 실행할 수 있는지 확인한다.

    loop_count 증가는 llm_reasoning_node가 담당하고,
    Engine은 추가 Loop 진입 가능 여부만 판단한다.
    """

    if state["loop_count"] >= MAX_LOOP:
        raise AgentEngineError(
            f"최대 ReAct Loop 횟수({MAX_LOOP})에 "
            "도달했습니다."
        )

def _apply_update(
    state: AgentRuntimeState,
    update: dict[str, Any],
) -> AgentRuntimeState:
    """
    Node가 반환한 변경값을 기존 상태에 병합한다.

    Node는 전체 상태를 반환하지 않고 변경된 값만 반환하므로,
    Engine에서 state.update()를 수행해야 한다.
    """

    state.update(update)
    return state

def _failure_update(
    state: AgentRuntimeState,
    message: str,
) -> AgentRuntimeState:
    """Engine 수준의 실패 상태를 만든다."""

    state.update(
        {
            "agent_outcome": "failed",
            "waiting_for_approval": False,
            "final_answer": None,
            "error": message,
        }
    )

    return state

def _is_failed(
    state: AgentRuntimeState,
) -> bool:
    """현재 Agent 실행이 실패 상태인지 확인한다."""

    return state["agent_outcome"] == "failed"


def is_waiting_for_approval(
    state: AgentRuntimeState,
) -> bool:
    """현재 사용자 승인을 기다리는 상태인지 확인한다."""

    return (
        state["agent_outcome"] == "awaiting_approval"
        and state["waiting_for_approval"] is True
        and state["scaling_required"] is True
        and state["scaling_plan"] is not None
    )


async def _run_node(
    state: AgentRuntimeState,
    node: AgentNode,
) -> AgentRuntimeState:
    """
    Node를 실행하고 반환된 변경값을 상태에 반영한다.
    """

    update = await node(state)

    if not isinstance(update, dict):
        node_name = getattr(
            node,
            "__name__",
            node.__class__.__name__,
        )
        raise AgentEngineError(
            f"{node.__name__}가 dict를 반환하지 않았습니다."
        )

    return _apply_update(state, update)

def _parse_revalidation_response(
    content: str,
) -> dict[str, Any]:
    """Solar가 반환한 재검증 JSON을 파싱한다."""

    cleaned = content.strip()

    if cleaned.startswith("```"):
        lines = cleaned.splitlines()

        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]

        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]

        cleaned = "\n".join(lines).strip()

    try:
        data = json.loads(cleaned)

    except json.JSONDecodeError as exc:
        raise AgentEngineError(
            "재검증 LLM 응답을 JSON으로 "
            f"변환하지 못했습니다: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise AgentEngineError(
            "재검증 LLM 응답의 최상위 값은 "
            "JSON 객체여야 합니다."
        )

    required_fields = {
        "summary",
        "performance_improved",
        "additional_action_required",
        "recommended_action",
    }

    missing_fields = required_fields - data.keys()

    if missing_fields:
        missing_text = ", ".join(
            sorted(missing_fields)
        )

        raise AgentEngineError(
            "재검증 응답 필드가 누락되었습니다: "
            f"{missing_text}"
        )

    if not isinstance(
        data["performance_improved"],
        bool,
    ):
        raise AgentEngineError(
            "performance_improved는 "
            "true 또는 false여야 합니다."
        )

    if not isinstance(
        data["additional_action_required"],
        bool,
    ):
        raise AgentEngineError(
            "additional_action_required는 "
            "true 또는 false여야 합니다."
        )

    return data

async def _revalidate_after_scaling(
    state: AgentRuntimeState,
    *,
    previous_load_test_result: LoadTestResult,
    previous_system_metrics: SystemMetrics,
    load_test_node: AgentNode,
    metrics_node: AgentNode,
    reasoning_node: AgentNode,
    revalidation_caller: RevalidationCaller,
) -> AgentRuntimeState:
    """
    스케일링 후 다시 부하 테스트와 메트릭 수집을 수행하고,
    이전 결과와 비교해 추가 조치 필요 여부를 판단한다.
    """

    # 1. 스케일링 후 부하 테스트 재실행
    state = await _run_node(
        state,
        load_test_node,
    )

    if _is_failed(state):
        return state

    # 2. 스케일링 후 메트릭 재수집
    state = await _run_node(
        state,
        metrics_node,
    )

    if _is_failed(state):
        return state

    current_load_test_result = state[
        "load_test_result"
    ]
    current_system_metrics = state[
        "system_metrics"
    ]

    if current_load_test_result is None:
        raise AgentEngineError(
            "재검증을 위한 load_test_result가 없습니다."
        )

    if current_system_metrics is None:
        raise AgentEngineError(
            "재검증을 위한 system_metrics가 없습니다."
        )

    # 3. 스케일링 전후 결과 비교 프롬프트 생성
    revalidation_prompt = build_revalidation_prompt(
        previous_load_test_result=(
            previous_load_test_result
        ),
        current_load_test_result=(
            current_load_test_result
        ),
        previous_system_metrics=(
            previous_system_metrics
        ),
        current_system_metrics=(
            current_system_metrics
        ),
    )

    # 4. Solar API를 통한 재검증
    content = await revalidation_caller(
        SYSTEM_PROMPT,
        revalidation_prompt,
    )

    revalidation = _parse_revalidation_response(
        content
    )

    summary = str(revalidation["summary"])

    performance_improved = revalidation[
        "performance_improved"
    ]

    additional_action_required = revalidation[
        "additional_action_required"
    ]

    recommended_action = revalidation.get(
        "recommended_action"
    )

    # 5. 성능 개선 완료
    if (
        performance_improved
        and not additional_action_required
    ):
        state.update(
            {
                "agent_outcome": "scaled",
                "scaling_required": False,
                "scaling_approved": True,
                "waiting_for_approval": False,
                "scaling_plan": None,
                "final_answer": (
                    "스케일링 후 재검증 완료: "
                    f"{summary}"
                ),
                "error": None,
            }
        )

        return state

    # 6. 개선 부족 시 다음 ReAct Loop 가능 여부 검사
    _ensure_loop_available(state)

    state.update(
        {
            "agent_outcome": "pending",
            "scaling_required": False,
            "scaling_approved": None,
            "waiting_for_approval": False,
            "scaling_plan": None,
            "final_answer": None,
            "error": None,
        }
    )

    # 7. 재측정된 상태로 다시 병목 분석
    state = await _run_node(
        state,
        reasoning_node,
    )

    if _is_failed(state):
        return state

    revalidation_message = (
        f"스케일링 후 재검증 결과: {summary}"
    )

    if recommended_action:
        revalidation_message += (
            " 추가 권장 조치: "
            f"{recommended_action}"
        )

    if state["final_answer"]:
        state["final_answer"] = (
            f"{revalidation_message} "
            f"{state['final_answer']}"
        )
    else:
        state["final_answer"] = (
            revalidation_message
        )

    return state

async def run_diagnosis(
    state: AgentRuntimeState,
    *,
    load_test_node: AgentNode | None = None,
    metrics_node: AgentNode | None = None,
    reasoning_node: AgentNode | None = None,
) -> AgentRuntimeState:
    """
    부하 테스트, 메트릭 수집, LLM 분석까지 실행한다.

    스케일링이 필요하지 않으면 diagnosed 상태로 종료하고,
    스케일링이 필요하면 awaiting_approval 상태로 반환한다.
    """
    load_test_node = (
    load_test_node or run_load_test_node
    )

    metrics_node = (
        metrics_node or collect_metrics_node
    )

    reasoning_node = (
        reasoning_node or llm_reasoning_node
    )

    try:
        async with asyncio.timeout(
            ENGINE_TIMEOUT_SECONDS
        ):  
            _ensure_loop_available(state)

            state = await _run_node(
                state,
                load_test_node,
            )

            if _is_failed(state):
                return state

            state = await _run_node(
                state,
                metrics_node,
            )

            if _is_failed(state):
                return state

            state = await _run_node(
                state,
                reasoning_node,
            )

            return state

    except TimeoutError:
        return _failure_update(
            state,
            (
                "Agent 진단 실행 제한 시간 "
                f"{ENGINE_TIMEOUT_SECONDS}초를 초과했습니다."
            ),
        )

    except Exception as exc:
        return _failure_update(
            state,
            f"Agent 진단 실행 실패: {exc}",
        )


async def start_agent(
    target_tps: int,
    duration: int,
    *,
    load_test_node: AgentNode | None = None,
    metrics_node: AgentNode | None = None,
    reasoning_node: AgentNode | None = None,
) -> AgentRuntimeState:
    """
    새로운 Agent 작업을 생성하고 최초 진단을 실행한다.

    반환 상태:
        diagnosed
            스케일링이 필요하지 않은 경우

        awaiting_approval
            스케일링이 필요해 사용자 승인을 기다리는 경우

        failed
            Node 실행 중 오류가 발생한 경우
    """

    state = create_initial_state(
        target_tps=target_tps,
        duration=duration,
    )

    try:
        async with asyncio.timeout(ENGINE_TIMEOUT_SECONDS):
            return await run_diagnosis(
                state,
                load_test_node=load_test_node,
                metrics_node=metrics_node,
                reasoning_node=reasoning_node,
                )

    except TimeoutError:
        return _failure_update(
            state,
            (
                "Agent 실행 제한 시간 "
                f"{ENGINE_TIMEOUT_SECONDS}초를 초과했습니다."
            ),
        )

    except Exception as exc:
        return _failure_update(
            state,
            f"Agent Engine 실행 실패: {exc}",
        )


async def resume_after_approval(
    state: AgentRuntimeState,
    approved: bool,
    *,
    scaling_node: AgentNode | None = None,
    load_test_node: AgentNode | None = None,
    metrics_node: AgentNode | None = None,
    reasoning_node: AgentNode | None = None,
    revalidation_caller: (
        RevalidationCaller | None
    ) = None,
) -> AgentRuntimeState:
    """
    승인 후 스케일링과 재검증을 수행한다.
    """

    scaling_node = (
        scaling_node or execute_scaling_node
    )

    load_test_node = (
        load_test_node or run_load_test_node
    )

    metrics_node = (
        metrics_node or collect_metrics_node
    )

    reasoning_node = (
        reasoning_node or llm_reasoning_node
    )

    revalidation_caller = (
        revalidation_caller or call_solar_api
    )

    if not is_waiting_for_approval(state):
        return _failure_update(
            state,
            (
                "현재 작업은 스케일링 승인 대기 "
                "상태가 아닙니다."
            ),
        )

    state["scaling_approved"] = approved
    state["waiting_for_approval"] = False

    if not approved:
        state.update(
            {
                "agent_outcome": "diagnosed",
                "scaling_required": False,
                "final_answer": (
                    "사용자가 스케일링 제안을 "
                    "거절했습니다. "
                    "진단 결과만 유지합니다."
                ),
                "error": None,
            }
        )
        return state

    # 스케일링 전 성능 보관
    previous_load_test_result = state[
        "load_test_result"
    ]
    previous_system_metrics = state[
        "system_metrics"
    ]

    if previous_load_test_result is None:
        return _failure_update(
            state,
            "스케일링 전 load_test_result가 없습니다.",
        )

    if previous_system_metrics is None:
        return _failure_update(
            state,
            "스케일링 전 system_metrics가 없습니다.",
        )

    try:
        async with asyncio.timeout(
            ENGINE_TIMEOUT_SECONDS
        ):
            # 실제 스케일링
            state = await _run_node(
                state,
                scaling_node,
            )

            if _is_failed(state):
                return state

            # 스케일링 후 재검증
            return await _revalidate_after_scaling(
                state,
                previous_load_test_result=(
                    previous_load_test_result
                ),
                previous_system_metrics=(
                    previous_system_metrics
                ),
                load_test_node=load_test_node,
                metrics_node=metrics_node,
                reasoning_node=reasoning_node,
                revalidation_caller=(
                    revalidation_caller
                ),
            )

    except TimeoutError:
        return _failure_update(
            state,
            (
                "스케일링 및 재검증 실행 제한 시간 "
                f"{ENGINE_TIMEOUT_SECONDS}초를 "
                "초과했습니다."
            ),
        )

    except Exception as exc:
        return _failure_update(
            state,
            (
                "스케일링 및 재검증 처리 실패: "
                f"{exc}"
            ),
        )