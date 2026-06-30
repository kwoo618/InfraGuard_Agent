import json
from typing import Any

# 이 프롬프트는 모든 병목 분석 요청에 공통으로 사용한다.
# 사용자가 입력한 데이터와 관계없이 LLM이 항상 지켜야 하는
# 역할, 판단 기준, 출력 규칙을 작성한다.
SYSTEM_PROMPT = """
당신은 클라우드 인프라와 DevOps 환경을 분석하는
InfraGuard Agent입니다.

당신의 역할은 Locust 부하 테스트 결과와 Prometheus 시스템
메트릭을 함께 분석하여 서버의 성능 병목 원인을 진단하는 것입니다.

분석할 때 다음 원칙을 반드시 지켜야 합니다.

1. TPS, 응답 지연 시간, 오류율, CPU 사용률, 메모리 사용률 등의
   수치 데이터를 근거로 판단합니다.

2. CPU 사용률 하나만으로 병목을 판단하지 않습니다.
   부하 테스트 결과와 시스템 메트릭을 종합적으로 분석합니다.

3. 병목 원인을 확실하게 판단하기 어려운 경우에는
   추측하지 말고 불확실하다고 표시합니다.

4. 무조건 스케일링을 제안하지 않습니다.
   실제로 성능 개선이 필요하다고 판단될 때만 제안합니다.

5. 스케일링이 필요한 경우에는 대상 서비스, 현재 컨테이너 수,
   목표 컨테이너 수와 판단 근거를 제공합니다.

6. 사용자의 승인 없이 직접 인프라를 변경하지 않습니다.

7. 응답은 반드시 요청된 JSON 형식으로만 반환합니다.
   JSON 외의 설명이나 마크다운 문법은 포함하지 않습니다.
""".strip()


def build_bottleneck_analysis_prompt(
    load_test_result: dict[str, Any],
    system_metrics: dict[str, Any],
) -> str:
    """
    Locust 부하 테스트 결과와 Prometheus 시스템 메트릭을
    Solar Pro가 분석할 수 있는 프롬프트로 변환한다.

    Parameters
    ----------
    load_test_result:
        Locust 부하 테스트를 통해 얻은 결과.

        예시:
        {
            "target_tps": 30,
            "actual_tps": 28.4,
            "average_latency_ms": 850,
            "p95_latency_ms": 2100,
            "error_rate": 0.11
        }

    system_metrics:
        Prometheus를 통해 수집한 시스템 리소스 정보.

        예시:
        {
            "cpu_usage": 91.5,
            "memory_usage": 63.2,
            "container_count": 1
        }

    Returns
    -------
    str
        Solar Pro에 전달할 병목 분석용 사용자 프롬프트.
    """

    # 파이썬 딕셔너리를 LLM이 읽기 쉬운 JSON 문자열로 변환한다.
    #
    # ensure_ascii=False:
    # 한글이 유니코드 형태로 변환되지 않고 그대로 출력되게 한다.
    #
    # indent=2:
    # JSON 문자열에 들여쓰기를 적용하여 읽기 쉽게 만든다.
    load_test_json = json.dumps(
        load_test_result,
        ensure_ascii=False,
        indent=2,
    )

    system_metrics_json = json.dumps(
        system_metrics,
        ensure_ascii=False,
        indent=2,
    )

    # 실제 측정 데이터와 LLM이 반환해야 할 JSON 구조를 함께 전달한다.
    return f"""
다음 Locust 부하 테스트 결과와 Prometheus 시스템 메트릭을
종합적으로 분석하여 서버의 성능 병목을 진단하세요.

[Locust 부하 테스트 결과]

{load_test_json}

[Prometheus 시스템 메트릭]

{system_metrics_json}

다음 내용을 분석하세요.

- 목표 TPS를 실제로 달성했는지
- 평균 응답 시간과 P95 응답 시간이 적절한지
- 오류율이 허용 가능한 수준인지
- CPU 또는 메모리 사용량이 과도한지
- 현재 문제가 일시적인 트래픽 증가인지 구조적인 병목인지
- 컨테이너 스케일링이 필요한지
- 추가 정보가 필요한지

반드시 다음 JSON 구조로만 응답하세요.

{{
  "summary": "전체 분석 결과를 요약한 문장",
  "bottleneck_detected": true,
  "bottleneck_type": "cpu",
  "confidence": 0.9,
  "evidence": [
    "병목 판단에 사용한 첫 번째 근거",
    "병목 판단에 사용한 두 번째 근거"
  ],
  "scaling_required": true,
  "scaling_plan": {{
    "service_name": "target-api",
    "current_replicas": 1,
    "target_replicas": 2,
    "reason": "스케일링이 필요한 이유"
  }},
  "additional_information_required": false,
  "additional_information_request": null
}}

각 필드의 작성 기준은 다음과 같습니다.

- summary:
  전체 진단 결과를 사용자가 이해할 수 있도록 요약합니다.

- bottleneck_detected:
  성능 병목이 발견되었다면 true,
  발견되지 않았다면 false를 반환합니다.

- bottleneck_type:
  다음 값 중 하나만 사용합니다.

  "cpu"
  "memory"
  "latency"
  "error_rate"
  "database"
  "network"
  "unknown"
  "none"

- confidence:
  분석 결과에 대한 신뢰도를 0.0부터 1.0 사이의 숫자로 반환합니다.

- evidence:
  병목을 판단한 구체적인 수치와 근거를 배열로 반환합니다.

- scaling_required:
  컨테이너 수 조정이 필요하면 true,
  필요하지 않으면 false를 반환합니다.

- scaling_plan:
  scaling_required가 true인 경우 스케일링 계획을 작성합니다.
  scaling_required가 false인 경우 null을 반환합니다.

- additional_information_required:
  현재 데이터만으로 병목 원인을 판단하기 어려우면 true를 반환합니다.

- additional_information_request:
  추가 정보가 필요하다면 어떤 정보가 필요한지 작성합니다.
  추가 정보가 필요하지 않으면 null을 반환합니다.
""".strip()


def build_revalidation_prompt(
    previous_load_test_result: dict[str, Any],
    current_load_test_result: dict[str, Any],
    previous_system_metrics: dict[str, Any],
    current_system_metrics: dict[str, Any],
) -> str:
    """
    스케일링 전후의 성능 데이터를 비교하기 위한 프롬프트를 생성한다.

    스케일링 실행 후 성능이 실제로 개선되었는지 판단할 때 사용한다.

    Parameters
    ----------
    previous_load_test_result:
        스케일링 전 Locust 부하 테스트 결과.

    current_load_test_result:
        스케일링 후 Locust 부하 테스트 결과.

    previous_system_metrics:
        스케일링 전 Prometheus 시스템 메트릭.

    current_system_metrics:
        스케일링 후 Prometheus 시스템 메트릭.

    Returns
    -------
    str
        스케일링 전후 성능 비교를 위한 프롬프트.
    """

    previous_load_test_json = json.dumps(
        previous_load_test_result,
        ensure_ascii=False,
        indent=2,
    )

    current_load_test_json = json.dumps(
        current_load_test_result,
        ensure_ascii=False,
        indent=2,
    )

    previous_metrics_json = json.dumps(
        previous_system_metrics,
        ensure_ascii=False,
        indent=2,
    )

    current_metrics_json = json.dumps(
        current_system_metrics,
        ensure_ascii=False,
        indent=2,
    )

    return f"""
다음은 컨테이너 스케일링 전후의 성능 측정 결과입니다.

각 데이터를 비교하여 스케일링으로 성능이 개선되었는지 분석하세요.

[스케일링 전 Locust 결과]

{previous_load_test_json}

[스케일링 후 Locust 결과]

{current_load_test_json}

[스케일링 전 시스템 메트릭]

{previous_metrics_json}

[스케일링 후 시스템 메트릭]

{current_metrics_json}

다음 항목을 비교하세요.

- 실제 처리 TPS 변화
- 평균 응답 시간 변화
- P95 응답 시간 변화
- 오류율 변화
- CPU 사용률 변화
- 메모리 사용률 변화
- 스케일링 조치의 전체적인 효과

반드시 다음 JSON 구조로만 응답하세요.

{{
  "summary": "스케일링 전후 비교 결과",
  "performance_improved": true,
  "tps_change": 0.0,
  "p95_latency_change_ms": 0.0,
  "error_rate_change": 0.0,
  "cpu_usage_change": 0.0,
  "evidence": [
    "성능 개선 또는 악화를 판단한 근거"
  ],
  "additional_action_required": false,
  "recommended_action": null
}}
""".strip()