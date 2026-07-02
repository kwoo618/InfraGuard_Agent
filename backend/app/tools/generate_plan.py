"""
병목 진단 결과를 바탕으로 사용자에게 제공할
최적화 조치 목록을 생성한다.

이 Tool은 인프라를 직접 변경하지 않는다.
실제 스케일링은 반드시 사용자 승인 후
execute_scaling_node를 통해 수행한다.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.schemas import (
    BottleneckReport,
    LoadTestResult,
    SystemMetrics,
)


class PlanGenerationError(ValueError):
    """최적화 플랜 생성 과정에서 발생한 오류."""


def _validate_scaling_plan(
    scaling_plan: Mapping[str, Any] | None,
) -> None:
    """스케일링 계획의 기본 구조를 검사한다."""

    if scaling_plan is None:
        return

    required_fields = {
        "service_name",
        "current_replicas",
        "desired_replicas",
        "reason",
    }

    missing_fields = required_fields - scaling_plan.keys()

    if missing_fields:
        missing_text = ", ".join(
            sorted(missing_fields)
        )
        raise PlanGenerationError(
            "scaling_plan 필드가 누락되었습니다: "
            f"{missing_text}"
        )

    try:
        current_replicas = int(
            scaling_plan["current_replicas"]
        )
        desired_replicas = int(
            scaling_plan["desired_replicas"]
        )
    except (TypeError, ValueError) as exc:
        raise PlanGenerationError(
            "replica 수는 정수여야 합니다."
        ) from exc

    if current_replicas < 1:
        raise PlanGenerationError(
            "current_replicas는 1 이상이어야 합니다."
        )

    if desired_replicas <= current_replicas:
        raise PlanGenerationError(
            "desired_replicas는 "
            "current_replicas보다 커야 합니다."
        )


def generate_optimization_plan(
    *,
    target_tps: int,
    load_test_result: LoadTestResult,
    system_metrics: SystemMetrics,
    bottleneck_report: BottleneckReport,
    scaling_plan: Mapping[str, Any] | None = None,
) -> list[str]:
    """
    진단 결과를 바탕으로 실행 가능한 최적화 조치 목록을 만든다.

    Parameters
    ----------
    target_tps:
        사용자가 요청한 목표 TPS.

    load_test_result:
        Locust 부하 테스트 결과.

    system_metrics:
        Prometheus 시스템 메트릭.

    bottleneck_report:
        LLM이 생성한 병목 진단 결과.

    scaling_plan:
        LLM이 생성한 스케일링 계획.
        스케일링이 필요하지 않으면 None이다.

    Returns
    -------
    list[str]
        사용자에게 보여줄 최적화 조치 목록.

    Raises
    ------
    PlanGenerationError
        입력값 또는 scaling_plan 구조가 잘못된 경우.
    """

    if target_tps <= 0:
        raise PlanGenerationError(
            "target_tps는 1 이상이어야 합니다."
        )

    if not isinstance(
        load_test_result,
        LoadTestResult,
    ):
        raise PlanGenerationError(
            "load_test_result는 "
            "LoadTestResult여야 합니다."
        )

    if not isinstance(
        system_metrics,
        SystemMetrics,
    ):
        raise PlanGenerationError(
            "system_metrics는 "
            "SystemMetrics여야 합니다."
        )

    if not isinstance(
        bottleneck_report,
        BottleneckReport,
    ):
        raise PlanGenerationError(
            "bottleneck_report는 "
            "BottleneckReport여야 합니다."
        )

    _validate_scaling_plan(scaling_plan)

    plans: list[str] = []

    # 1. LLM의 대표 권장 조치를 가장 먼저 추가한다.
    recommendation = (
        bottleneck_report.recommendation.strip()
    )

    if recommendation:
        plans.append(recommendation)

    # 2. 목표 TPS 미달 대응
    if load_test_result.tps < target_tps:
        plans.append(
            "현재 TPS "
            f"{load_test_result.tps:.1f}를 "
            f"목표 TPS {target_tps} 이상으로 "
            "높이기 위한 병목 구간을 점검합니다."
        )

    # 3. 응답 지연 대응
    if load_test_result.latency_p95 >= 1000:
        plans.append(
            "P95 응답 시간이 "
            f"{load_test_result.latency_p95:.1f}ms로 "
            "높으므로 느린 요청과 애플리케이션 "
            "처리 구간을 점검합니다."
        )
    elif load_test_result.latency_p95 >= 500:
        plans.append(
            "P95 응답 시간 "
            f"{load_test_result.latency_p95:.1f}ms를 "
            "줄이기 위해 요청 처리 성능을 점검합니다."
        )

    # 4. 오류율 대응
    if load_test_result.error_rate >= 0.05:
        plans.append(
            "오류율이 "
            f"{load_test_result.error_rate * 100:.1f}%로 "
            "높으므로 서버 오류 로그와 실패 "
            "엔드포인트를 우선 분석합니다."
        )
    elif load_test_result.error_rate > 0:
        plans.append(
            "발생한 요청 오류 "
            f"{load_test_result.error_rate * 100:.1f}%의 "
            "원인을 확인합니다."
        )

    # 5. CPU 대응
    if system_metrics.cpu_pct >= 85:
        plans.append(
            "CPU 사용률이 "
            f"{system_metrics.cpu_pct:.1f}%이므로 "
            "CPU 사용량이 높은 프로세스와 "
            "요청 처리 로직을 점검합니다."
        )

    # 6. 메모리 대응
    if system_metrics.mem_pct >= 85:
        plans.append(
            "메모리 사용률이 "
            f"{system_metrics.mem_pct:.1f}%이므로 "
            "메모리 누수와 캐시 사용량을 점검합니다."
        )

    # 7. 연결 수 대응
    if system_metrics.connection_count >= 100:
        plans.append(
            "활성 연결 수가 "
            f"{system_metrics.connection_count}개이므로 "
            "커넥션 풀과 연결 제한 설정을 점검합니다."
        )

    # 8. 스케일링 계획
    if bottleneck_report.requires_scaling:
        if scaling_plan is None:
            raise PlanGenerationError(
                "스케일링이 필요한 경우 "
                "scaling_plan이 필요합니다."
            )

        service_name = str(
            scaling_plan["service_name"]
        )
        current_replicas = int(
            scaling_plan["current_replicas"]
        )
        desired_replicas = int(
            scaling_plan["desired_replicas"]
        )

        plans.insert(
            0,
            f"{service_name} 서비스를 "
            f"{current_replicas}개에서 "
            f"{desired_replicas}개로 확장합니다. "
            "실행 전 사용자 승인이 필요합니다.",
        )

    elif scaling_plan is not None:
        raise PlanGenerationError(
            "스케일링이 불필요한 경우 "
            "scaling_plan은 None이어야 합니다."
        )

    # 완전히 정상인 경우에도 빈 목록을 반환하지 않는다.
    if not plans:
        plans.append(
            "현재 성능을 유지하고 주기적으로 "
            "부하 테스트와 시스템 메트릭을 "
            "모니터링합니다."
        )

    # 같은 메시지가 여러 번 들어간 경우 순서를 유지하며 제거한다.
    return list(dict.fromkeys(plans))