# 박정기 - LLM reasoning 

"""
InfraGuard Agent의 실행 단계를 담당하는 Node 모음.

각 Node는 AgentRuntimeState를 입력받고,
변경할 상태 값만 딕셔너리 형태로 반환한다.

실행 흐름:
    run_load_test_node
        → collect_metrics_node
        → llm_reasoning_node
        → 사용자 승인
        → execute_scaling_node
"""

import asyncio
import json
import os

from dotenv import load_dotenv
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.agent.prompts import (
    SYSTEM_PROMPT,
    build_bottleneck_analysis_prompt,
)
from app.agent.state import AgentOutcome, AgentRuntimeState
from app.schemas import (
    BottleneckReport,
    LoadTestResult,
    ScalingResult,
    SystemMetrics,
)
from app.tools.get_metrics import get_system_metrics
from app.tools.run_load_test import run_load_test
from app.tools.scale_service import (
    SERVICE_NAME,
    get_current_replicas,
    scale_service,
)

load_dotenv()
# Solar API 설정
UPSTAGE_API_KEY = os.getenv("UPSTAGE_API_KEY", "")
UPSTAGE_API_URL = os.getenv(
    "UPSTAGE_API_URL",
    "https://api.upstage.ai/v1/solar/chat/completions",
)
UPSTAGE_MODEL = os.getenv(
    "UPSTAGE_MODEL",
    "solar-pro",
)

# ReAct Loop의 무한 반복을 막기 위한 상한
MAX_LOOP = int(os.getenv("MAX_LOOP", "10"))


# 테스트에서 실제 API 대신 Mock 함수를 전달할 수 있도록 정의한 타입
LLMCaller = Callable[[str, str], Awaitable[str]]


class NodeExecutionError(RuntimeError):
    """Agent Node 실행 과정에서 발생한 오류."""


def _create_failure_update(
    message: str,
) -> dict[str, Any]:
    """
    Node에서 오류가 발생했을 때 공통으로 반환할 상태 변경값을 만든다.
    """

    return {
        "agent_outcome": "failed",
        "waiting_for_approval": False,
        "final_answer": None,
        "error": message,
    }


def _get_current_replicas() -> int:
    """
    현재 실행 중인 target-server replica 수를 조회한다.

    Docker가 아직 실행되지 않아 조회 결과가 0이면,
    프롬프트 생성을 위해 최소값 1을 사용한다.
    """

    replica_count = get_current_replicas()
    return max(replica_count, 1)


def _remove_markdown_code_block(content: str) -> str:
    """
    LLM이 JSON을 Markdown 코드 블록으로 감싸 반환한 경우 제거한다.

    예:
        ```json
        {"result": true}
        ```
    """

    cleaned = content.strip()

    if not cleaned.startswith("```"):
        return cleaned

    lines = cleaned.splitlines()

    # 첫 번째 ```json 또는 ``` 제거
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]

    # 마지막 ``` 제거
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]

    return "\n".join(lines).strip()


def _parse_llm_json(content: str) -> dict[str, Any]:
    """
    LLM 문자열 응답을 Python 딕셔너리로 변환한다.
    """

    cleaned_content = _remove_markdown_code_block(content)

    try:
        parsed = json.loads(cleaned_content)
    except json.JSONDecodeError as exc:
        raise NodeExecutionError(
            f"LLM 응답을 JSON으로 변환하지 못했습니다: {exc}"
        ) from exc

    if not isinstance(parsed, dict):
        raise NodeExecutionError(
            "LLM 응답의 최상위 값은 JSON 객체여야 합니다."
        )

    return parsed


def _create_bottleneck_report(
    response_data: dict[str, Any],
) -> BottleneckReport:
    """
    LLM 응답의 bottleneck_report를
    schemas.py의 BottleneckReport 객체로 변환한다.
    """

    report_data = response_data.get("bottleneck_report")

    if not isinstance(report_data, dict):
        raise NodeExecutionError(
            "LLM 응답에 bottleneck_report 객체가 없습니다."
        )

    required_fields = {
        "cause",
        "severity",
        "recommendation",
        "confidence",
        "requires_scaling",
    }

    missing_fields = required_fields - report_data.keys()

    if missing_fields:
        missing_text = ", ".join(sorted(missing_fields))

        raise NodeExecutionError(
            f"bottleneck_report 필드가 누락되었습니다: {missing_text}"
        )

    severity = report_data["severity"]

    if severity not in {"low", "medium", "high"}:
        raise NodeExecutionError(
            "severity는 low, medium, high 중 하나여야 합니다."
        )

    try:
        confidence = float(report_data["confidence"])
    except (TypeError, ValueError) as exc:
        raise NodeExecutionError(
            "confidence는 숫자여야 합니다."
        ) from exc

    if not 0.0 <= confidence <= 1.0:
        raise NodeExecutionError(
            "confidence는 0.0부터 1.0 사이여야 합니다."
        )

    requires_scaling = report_data["requires_scaling"]

    if not isinstance(requires_scaling, bool):
        raise NodeExecutionError(
            "requires_scaling은 true 또는 false여야 합니다."
        )

    return BottleneckReport(
        cause=str(report_data["cause"]),
        severity=severity,
        recommendation=str(report_data["recommendation"]),
        confidence=confidence,
        requires_scaling=requires_scaling,
    )


def _validate_agent_outcome(
    outcome: Any,
    requires_scaling: bool,
) -> AgentOutcome:
    """
    LLM이 반환한 agent_outcome과 requires_scaling의 관계를 검사한다.
    """

    expected_outcome: AgentOutcome

    if requires_scaling:
        expected_outcome = "awaiting_approval"
    else:
        expected_outcome = "diagnosed"

    if outcome != expected_outcome:
        raise NodeExecutionError(
            "agent_outcome이 requires_scaling 값과 일치하지 않습니다. "
            f"예상값: {expected_outcome}, 반환값: {outcome}"
        )

    return expected_outcome


def _validate_scaling_plan(
    scaling_plan: Any,
    *,
    service_name: str,
    current_replicas: int,
    requires_scaling: bool,
) -> dict[str, object] | None:
    """
    LLM이 생성한 scaling_plan의 필드와 값이 올바른지 검사한다.
    """

    if not requires_scaling:
        if scaling_plan is not None:
            raise NodeExecutionError(
                "requires_scaling이 false이면 scaling_plan은 null이어야 합니다."
            )

        return None

    if not isinstance(scaling_plan, dict):
        raise NodeExecutionError(
            "스케일링이 필요한 경우 scaling_plan 객체가 필요합니다."
        )

    required_fields = {
        "service_name",
        "current_replicas",
        "desired_replicas",
        "reason",
    }

    missing_fields = required_fields - scaling_plan.keys()

    if missing_fields:
        missing_text = ", ".join(sorted(missing_fields))

        raise NodeExecutionError(
            f"scaling_plan 필드가 누락되었습니다: {missing_text}"
        )

    if scaling_plan["service_name"] != service_name:
        raise NodeExecutionError(
            "scaling_plan의 service_name이 실제 서비스 이름과 다릅니다."
        )

    try:
        plan_current_replicas = int(
            scaling_plan["current_replicas"]
        )
        desired_replicas = int(
            scaling_plan["desired_replicas"]
        )
    except (TypeError, ValueError) as exc:
        raise NodeExecutionError(
            "current_replicas와 desired_replicas는 정수여야 합니다."
        ) from exc

    if plan_current_replicas != current_replicas:
        raise NodeExecutionError(
            "scaling_plan의 current_replicas가 실제 값과 다릅니다."
        )

    if desired_replicas <= current_replicas:
        raise NodeExecutionError(
            "desired_replicas는 current_replicas보다 커야 합니다."
        )

    return {
        "service_name": service_name,
        "current_replicas": current_replicas,
        "desired_replicas": desired_replicas,
        "reason": str(scaling_plan["reason"]),
    }


async def call_solar_api(
    system_prompt: str,
    user_prompt: str,
) -> str:
    """
    Upstage Solar API를 호출하고 LLM의 문자열 응답을 반환한다.

    환경변수:
        UPSTAGE_API_KEY
        UPSTAGE_API_URL
        UPSTAGE_MODEL
    """

    if not UPSTAGE_API_KEY:
        raise NodeExecutionError(
            "UPSTAGE_API_KEY 환경변수가 설정되지 않았습니다."
        )

    headers = {
        "Authorization": f"Bearer {UPSTAGE_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": UPSTAGE_MODEL,
        "messages": [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": user_prompt,
            },
        ],
        "temperature": 0.1,
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                UPSTAGE_API_URL,
                headers=headers,
                json=payload,
            )

            response.raise_for_status()

    except httpx.HTTPStatusError as exc:
        response_text = exc.response.text[:300]

        raise NodeExecutionError(
            "Solar API가 오류 응답을 반환했습니다. "
            f"status={exc.response.status_code}, "
            f"response={response_text}"
        ) from exc

    except httpx.HTTPError as exc:
        raise NodeExecutionError(
            f"Solar API 호출에 실패했습니다: {exc}"
        ) from exc

    try:
        response_data = response.json()
        content = response_data["choices"][0]["message"]["content"]
    except (
        json.JSONDecodeError,
        KeyError,
        IndexError,
        TypeError,
    ) as exc:
        raise NodeExecutionError(
            "Solar API 응답 형식이 예상한 구조와 다릅니다."
        ) from exc

    if not isinstance(content, str) or not content.strip():
        raise NodeExecutionError(
            "Solar API가 빈 응답을 반환했습니다."
        )

    return content


async def run_load_test_node(
    state: AgentRuntimeState,
    load_test_runner: Callable[..., LoadTestResult] = run_load_test,
) -> dict[str, Any]:
    """
    Locust 부하 테스트를 실행한다.

    run_load_test()는 동기 함수이므로 asyncio.to_thread()를 사용해
    FastAPI의 이벤트 루프가 멈추지 않도록 한다.
    """

    try:
        result = await asyncio.to_thread(
            load_test_runner,
            target_tps=state["target_tps"],
            duration=state["duration"],
        )

        if not isinstance(result, LoadTestResult):
            raise NodeExecutionError(
                "run_load_test가 LoadTestResult를 반환하지 않았습니다."
            )

        return {
            "load_test_result": result,
            "agent_outcome": "pending",
            "error": None,
        }

    except Exception as exc:
        return _create_failure_update(
            f"부하 테스트 실행 실패: {exc}"
        )


async def collect_metrics_node(
    state: AgentRuntimeState,
    metrics_collector: Callable[
        [], Awaitable[SystemMetrics]
    ] = get_system_metrics,
) -> dict[str, Any]:
    """
    Prometheus에서 시스템 메트릭을 수집한다.
    """

    try:
        result = await metrics_collector()

        if not isinstance(result, SystemMetrics):
            raise NodeExecutionError(
                "get_system_metrics가 SystemMetrics를 반환하지 않았습니다."
            )

        return {
            "system_metrics": result,
            "agent_outcome": "pending",
            "error": None,
        }

    except Exception as exc:
        return _create_failure_update(
            f"시스템 메트릭 수집 실패: {exc}"
        )


async def llm_reasoning_node(
    state: AgentRuntimeState,
    llm_caller: LLMCaller = call_solar_api,
) -> dict[str, Any]:
    """
    Locust 결과와 Prometheus 메트릭을 LLM에 전달하고,
    병목 진단 결과와 스케일링 계획을 상태에 저장한다.
    """

    try:
        if state["loop_count"] >= MAX_LOOP:
            raise NodeExecutionError(
                f"최대 ReAct Loop 횟수({MAX_LOOP})에 도달했습니다."
            )

        load_test_result = state["load_test_result"]
        system_metrics = state["system_metrics"]

        if load_test_result is None:
            raise NodeExecutionError(
                "LLM 분석 전에 load_test_result가 필요합니다."
            )

        if system_metrics is None:
            raise NodeExecutionError(
                "LLM 분석 전에 system_metrics가 필요합니다."
            )

        current_replicas = _get_current_replicas()

        user_prompt = build_bottleneck_analysis_prompt(
            target_tps=state["target_tps"],
            load_test_result=load_test_result,
            system_metrics=system_metrics,
            service_name=SERVICE_NAME,
            current_replicas=current_replicas,
        )

        content = await llm_caller(
            SYSTEM_PROMPT,
            user_prompt,
        )

        response_data = _parse_llm_json(content)

        report = _create_bottleneck_report(
            response_data
        )

        outcome = _validate_agent_outcome(
            response_data.get("agent_outcome"),
            report.requires_scaling,
        )

        scaling_plan = _validate_scaling_plan(
            response_data.get("scaling_plan"),
            service_name=SERVICE_NAME,
            current_replicas=current_replicas,
            requires_scaling=report.requires_scaling,
        )

        if report.requires_scaling:
            desired_replicas = scaling_plan["desired_replicas"]

            final_answer = (
                f"{report.cause} "
                f"{SERVICE_NAME} 서비스를 "
                f"{current_replicas}개에서 "
                f"{desired_replicas}개로 확장할 것을 제안합니다. "
                "실행하려면 사용자 승인이 필요합니다."
            )
        else:
            final_answer = (
                f"진단 완료: {report.cause} "
                f"권장 조치: {report.recommendation}"
            )

        return {
            "bottleneck_report": report,
            "agent_outcome": outcome,
            "scaling_plan": scaling_plan,
            "scaling_required": report.requires_scaling,
            "scaling_approved": None,
            "waiting_for_approval": report.requires_scaling,
            "loop_count": state["loop_count"] + 1,
            "final_answer": final_answer,
            "error": None,
        }

    except Exception as exc:
        failure_update = _create_failure_update(
            f"LLM 병목 분석 실패: {exc}"
        )

        failure_update["loop_count"] = state["loop_count"] + 1
        return failure_update


async def execute_scaling_node(
    state: AgentRuntimeState,
    scaler: Callable[[int], Awaitable[ScalingResult]] = scale_service,
) -> dict[str, Any]:
    """
    사용자가 승인한 경우에만 Docker Compose 스케일링을 실행한다.
    """

    try:
        if state["scaling_approved"] is not True:
            raise NodeExecutionError(
                "사용자 승인 없이 스케일링을 실행할 수 없습니다."
            )

        scaling_plan = state["scaling_plan"]

        if scaling_plan is None:
            raise NodeExecutionError(
                "실행할 scaling_plan이 없습니다."
            )

        desired_replicas_value = scaling_plan.get(
            "desired_replicas"
        )

        try:
            desired_replicas = int(
                desired_replicas_value
            )
        except (TypeError, ValueError) as exc:
            raise NodeExecutionError(
                "desired_replicas는 정수여야 합니다."
            ) from exc

        result = await scaler(desired_replicas)

        if not isinstance(result, ScalingResult):
            raise NodeExecutionError(
                "scale_service가 ScalingResult를 반환하지 않았습니다."
            )

        if not result.success:
            return {
                "scaling_result": result,
                "agent_outcome": "failed",
                "waiting_for_approval": False,
                "final_answer": (
                    "스케일링 실행에 실패했습니다: "
                    f"{result.error_message or '원인을 확인할 수 없습니다.'}"
                ),
                "error": (
                    result.error_message
                    or "스케일링 실행 실패"
                ),
            }

        return {
            "scaling_result": result,
            "agent_outcome": "scaled",
            "waiting_for_approval": False,
            "scaling_count": state["scaling_count"] + 1,
            "final_answer": (
                f"{SERVICE_NAME} 서비스를 "
                f"{result.before_replicas}개에서 "
                f"{result.after_replicas}개로 확장했습니다."
            ),
            "error": None,
        }

    except Exception as exc:
        return _create_failure_update(
            f"스케일링 실행 실패: {exc}"
        )